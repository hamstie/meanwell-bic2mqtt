#!/usr/bin/env python3
APP_VER = "0.0"
APP_NAME = "bic2mqtt"

"""
 fst:05.04.2024 lst:05.04.2024
 Meanwell BIC2200-XXCAN to mqtt bridge
 V0.0  No fuction yet

"""

import logging
#from logging.handlers import RotatingFileHandler

from cmqtt import CMQTT
#from cbic2200 import CBic @todo

from datetime import datetime
import time
import os.path
import json
import configparser

#import sys
#import argparse
#import os
#import subprocess

# later we modify this, using a config file 
MQTT_BROKER_ADR = "127.0.0.1" # mqtt broker ip-address
MQTT_USER =""
MQTT_PASSWD = ""
MQTT_APP_ID = "0" # more than one instance ?

MQTT_T_MAIN = "haus/power/bat" # main topic
MQTT_T_APP = MQTT_T_MAIN + '/' + APP_NAME.lower() # app topic main: this one can be changed for each instance

# global objects
mqttc = None
ini = None # Ini-Config parser
app = None # main application
lg = None # logger

# simple config/ini file parser
class CIni:
	def __init__(self,fname : str):
		self.fname = fname # ini file name
		self.cfg = configparser.ConfigParser()
		lret = self.cfg.read(self.fname)
		if len(lret) == 0:
			raise FileNotFoundError(fname)

	def get_str(self,sec : str,key : str,def_val : str):
		#if self.cfg.has_section(sec):
		if self.cfg.has_option(sec, key):
			ret = self.cfg.get(sec,key).strip('"')
			ret = ret.strip(' ')
			return ret
		return def_val

	def get_int(self,sec : str,key : str,def_val : int):
		try:
			ret=int(self.get_str(sec,key,str(def_val)))
			return int(ret)
		except ValueError:
			pass
		return def_val


"""
BIC Device Object:
 - config parameter 
 - state of bic
 - charge control
"""
class CBicDevBase():
	def __init__(self,id : int,type : str):
		self.id = id	# device-id from ini
		self.type = "BIC22XX-XXBASE" # will be ovrwritten
		self.info = {}
		self.info['id'] = int(self.id)
		self.info['type'] = self.type
		self.info['fault'] = {'av':0,'eeprom':0,'fan':0,'temp':0,'dcV':0,'dcA':0,'shortC':0,'acGrid':0,'pfc':0}
		self.info['tempC'] = -278
	
		self.state = {}
		self.state['onlMode'] = "on" # [on,off]
		self.state['acGridV'] = 0 # grid-volatge [V]
		self.state['dcBatV'] = 0 # bat voltage DV [V]
		self.state['batCapPc'] = 0 # bat capacity [%] 
		self.state['dcBatA'] = 0  # bat [A] discharge[-] charge[+] ?
		self.state['changeMode'] = "discharge" # [charge,discharge,none]

		self.can_bit_rate = 0 # canbus baud-rate
		self.can_adr = 0 # can address
		self.can_dev = "" # can device node /dev/tty... for serial can interface
		self.cfg_max_vcharge100 = 0
		self.cfg_min_vdischarge100 = 6000
		self.cfg_max_ccharge100 = 0
		self.cfg_max_cdischarge100 = 0

	""" ini file config parameter
		@param dbkey-int [DEVICE]Id/X/ChargeVoltage def:2750 volt*100
		@param dbkey-int [DEVICE]Id/X/DischargeVoltage def:2520 volt*100
		@param dbkey-int [DEVICE]Id/X/MaxChargeCurrent def:3500 volt*100
		@param dbkey-int [DEVICE]Id/X/MaxDischargeCurrent def:2600 volt*100
	"""
	def cfg(self,ini):

		def kpfx():
			return "Id/{}/".format(self.id)

		self.cfg_max_vcharge100 = ini.get_int('DEVICE',kpfx() + "ChargeVoltage",self.cfg_max_vcharge100)
		self.cfg_min_vdischarge100 = ini.get_int('DEVICE',kpfx() + "DischargeVoltage",self.cfg_min_vdischarge100)
		self.cfg_max_ccharge100 = ini.get_int('DEVICE',kpfx() + "MaxChargeCurrent",self.cfg_max_ccharge100)
		self.cfg_max_cdischarge100 = ini.get_int('DEVICE',kpfx() + "MaxDischargeCurrent",self.cfg_max_cdischarge100)
		lg.info("init " + str(self))
		#dischargedelay = int(config.get('Settings', 'DischargeDelay'))

	def __str__(self):
		return "dev id:{} cfg-cv:{} cfg-dv:{} cc:{} cfg-dc:{}".format(self.id,self.cfg_max_vcharge100,self.cfg_min_vdischarge100,self.cfg_max_ccharge100,self.cfg_max_cdischarge100)

	def poll(self,timeslive_ms):
		pass

# device type 2200-24V CAN
class CBicDev2200_24(CBicDevBase):
	
	def __init__(self,id : int):
		super().__init__(id,"BIC2200-24CAN")
		self.can_bit_rate = 250000 # canbus bit-rate
		self.can_adr = 0x000C0300 # can address

		self.cfg_vcharge100 = 2750
		self.cfg_vdischarge100 = 2520
		self.cfg_max_charge100 = 3500
		self.cfg_max_cdischarge100 = 2600


	# special config
	def cfg(self,ini):
		super().cfg(ini)
		if self.id >0:
			return 0
		else:
			return -1
		
	def poll(self,timeslive_ms):
		super().poll(timeslive_ms)	

class App:
	ts_1000ms=0 # [ms]
	ts_6sec=0   # [s]
	ts_1min=0   # [s]

	def __init__(self,cmqtt):
		self.cmqtt = cmqtt
		#self.ini = ini
		#self.id= ini.get_str('MQTT','AppId',MQTT_APP_ID)
		self.t_start =  datetime.now()   # time.localtime()  
		self.info = {}
		self.started = False
		self.con_time_min=0
		self.dev_bic = {} # all bic hardware devices
		
	""" BIC Config
		[DEVICE]
		@param dbkey-str [MODEM]Id/X/Type def:empty well known modem type "BIC2200"
		@param dbkey-int [MODEM]Id/X/CanBaudRate def:0 Baudrate
		@topic-sub <main-app>/bic/<id>/set some bic charge/discharge regulation values {"chargeC100","20"} or {"dischargeC100","20"}
	"""
	def cfg(self,ini):
		# @future-use iterate over all bic's
		id = 0
		dev_type = ini.get_str('DEVICE','Id/{}/Type'.format(id),"")
		if dev_type == 'BIC2200-24CAN':
			dev = CBicDev2200_24(id)
			dev.cfg(ini)
			self.dev_bic[id] = dev
			if len(self.dev_bic) >0:
				mqttc.append_subscribe_topic(MQTT_T_APP + "/bic/{}/set".format(id),self.cb_mqtt_sub_event)

		
	# @todo set a bic value 
	def cb_mqtt_sub_event(self,mqttc,user_data,mqtt_msg):
		print('on subsc:' + mqtt_msg.pp())

	def start(self):
		if self.started is False:
			self.started=True
			#self.mqttc.publish(MQTT_T_APP + '/state/devall',json.dumps(lst_dev_cfg, sort_keys=False, indent=4),0,True) # retained

	def poll(self,timeslice_ms):
		if self.started is False:
				pass

		App.ts_1000ms+=timeslice_ms
		if App.ts_1000ms > 1000:
			App.ts_1000ms=0
			for dev in self.dev_bic.values():
				dev.poll(1000)
				
			App.ts_6sec+=1
			if App.ts_6sec >6:
				App.ts_6sec=0

			App.ts_1min+=1
			if App.ts_1min >59:
				App.ts_1min=0
				self.con_time_min+=1
				mqttc.publish(MQTT_T_APP,self.json_encode(),0,True)
					
	def json_encode(self):
		self.info['appVer'] = APP_VER
		self.info['appName'] = APP_NAME
		self.info['startTS'] =  self.t_start.strftime('%y%m%d_%H:%M:%S')   #  time.strftime("%y%m%d_%H:%M:%S",self.t_start)
		self.info['ts'] = datetime.now().strftime('%y%m%d_%H:%M:%S.%f')[:-3]
		self.info['conTimeMin'] = self.con_time_min 
		self.info['conCnt'] = mqttc.conn_cnt
		return json.dumps(self.info, sort_keys=False, indent=4)


# The callback for when the client receives a CONNACK response from the server.
def mqtt_on_connect(mqtt,userdata):
	global app
	lg.info("mqtt ✔connected:" + str(mqtt.id))
	mqtt.publish(MQTT_T_APP,app.json_encode(),0,True) # publish the state
	app.start()

# mqtt disconnected
def mqtt_on_disconnect(mqtt,userdata, rc):
    lg.info("mqtt disconnected from broker "+ str(rc))

""" main config
[DEVICE]
@param dbkey-str [MQTT]BrokerIpAdr def:"127.0.0.1" 
@param dbkey-str [MQTT]BrokerUser def:""
@param dbkey-str [MQTT]BrokerPasswd def:""
@param dbkey-str [MQTT]TopicMain def:""

@topic sub <main-app>/sys/state lwt [offline,running]
"""
def main_init():
	global ini
	ini = CIni("./" + APP_NAME + '.ini')
	
	global lg
	tl = logging.INFO
	str_tr=ini.get_str('ALL','TraceLevel',"").lower()
	if str_tr == 'debug':
			tl = logging.DEBUG

		
	lst_log_handler=[logging.StreamHandler()] # default log to console
	str_tfp=ini.get_str('ALL','TraceFilePath',"")
	if len(str_tfp) >0:
			lst_log_handler.append(logging.FileHandler(filename=str_tfp + '/' + APP_NAME + '.log', mode='a'))
			#lst_log_handler.append(RotatingFileHandler(filename=str_tfp + '/' + APP_NAME + '2.log', mode='w',maxBytes=512000,backupCount=4))

	logging.basicConfig(level=tl,format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%y-%m-%d %H:%M:%S',handlers=lst_log_handler)
	logging.addLevelName( logging.WARNING, "\033[1;31m%s\033[1;0m" % logging.getLevelName(logging.WARNING))
	logging.addLevelName( logging.ERROR, "\033[1;41m%s\033[1;0m" % logging.getLevelName(logging.ERROR))
	lg = logging.getLogger()
	lg.setLevel(tl)
	
	global mqttc
	mqttc = CMQTT(ini.get_str('MQTT','AppId',MQTT_APP_ID),None)
	mqttc.app_user = ini.get_str('MQTT','BrokerAccUser',MQTT_USER)
	mqttc.app_passwd = ini.get_str('MQTT','BrokerAccPasswd',MQTT_PASSWD)
	mqttc.app_ip_adr = ini.get_str('MQTT','BrokerIpAdr',MQTT_BROKER_ADR)
	mqttc.set_auth(mqttc.app_user,mqttc.app_passwd)
	global MQTT_T_APP
	MQTT_T_APP = ini.get_str('MQTT','TopicMain',MQTT_T_APP)  
	mqttc.on_connect = mqtt_on_connect
	mqttc.set_lwt(MQTT_T_APP + '/sys/state','offline','running')
	mqttc.on_disconnect = mqtt_on_disconnect
	
	
	global app
	app = App(mqttc)
	app.cfg(ini)


def main_exit():
	# @todo save can shutdown
	exit(0)

if __name__ == "__main__":
	main_init()
	if mqttc is not None and len(mqttc.app_ip_adr):
		try:
			mqttc.connect(str(mqttc.app_ip_adr))
		except:
			logging.info("mqtt\tcan't connect to broker:" + MQTT_BROKER_ADR)        
			exit (-1)


	poll_time_slice_ms=20
	poll_time_slice_sec=poll_time_slice_ms/1000 # 20ms
	while True:
		app.poll(poll_time_slice_ms)
		try:
			time.sleep(poll_time_slice_sec)
		except KeyboardInterrupt:
			main_exit(None,None)


