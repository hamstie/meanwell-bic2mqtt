#!/usr/bin/env python3
APP_VER = "0.40"
APP_NAME = "bic2mqtt"

"""
 fst:05.04.2024 lst:02.05.2024
 Meanwell BIC2200-XXCAN to mqtt bridge
 V0.40 ..pid-charge control
 V0.33 -refresh info every hour
 V0.32 -bugfixing
 V0.31 -cleaning charger code
		- op-mode 0->1 set charge to 0
		+ discharge block interval
 V0.30 -charge control works
 V0.22 -charge control testing
 V0.10 charge and discharging is possible for device BIC2200-24-CAN
 V0.04 cbic2200 first tests
 V0.01 mqtt is running
 V0.00 No fuction yet, working on the app-frame

 @todo: P1: check what happened if the broker is unreachable
	    P2: (Toggling display string for MQTT-Dashboards: Power,Temp,Voltage..)
		P1: DischargeBlockTime Start/Stop

 - EEPROM Write is possible since datecode:2402..
"""

import logging
#from logging.handlers import RotatingFileHandler

from cmqtt import CMQTT
from cbic2200 import CBic

from datetime import datetime
import time
import os.path
import json
import configparser
from cavg import CMAvg

import sys
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
		#print('nf:' + str(key) + str(self.cfg.options(sec)))
		return def_val

	def get_int(self,sec : str,key : str,def_val : int):
		try:
			ret=int(self.get_str(sec,key,str(def_val)))
			return int(ret)
		except ValueError:
			pass
		return def_val

	def get_float(self,sec : str,key : str,def_val : int):
		try:
			ret=float(self.get_str(sec,key,str(def_val)))
			return float(ret)
		except ValueError:
			pass
		return def_val

	# @return a dic of key,values for the given section
	def get_sec_keys(self,sec : str):
		ret = {}
		if self.cfg.has_section(sec):
			ret = dict(self.cfg.items(sec))
		return ret

class CBattery():
	def __init__(self,id):
		self.d_Cap2V = {} # key: capacity in %  [0..100], value: voltage*100
		self.d_Cap2V[0]=0

	def check(self):
		k_old = 0
		v_old = 0
		for k,v in self.d_Cap2V.items():
			if k_old > k or v_old > v:
				raise RuntimeError('wrong/mismatch bat table entry' + str(self.d_Cap2V))
			#print('{}%={}'.format(k,v))
		return 0

	# bat profile from ini
	# @param dbkey-int [BAT_0]Cap2V/X=V battery capacity [%] to voltage
	def cfg(self,ini):
		d = ini.get_sec_keys('BAT_0')
		for k,v in d.items():
				if k.find('cap2v/')>=0:
					cap_pc = int(k.replace('cap2v/',''))
					self.d_Cap2V[cap_pc] = float(v)
		self.check()

	# @return the capacity of the battery [%]
	def get_capacity_pc(self,volt):

		#@audit approx values between two cap values in the list
		def approx(c1:float ,c2:float,v1:float,v2:float):

			#straight line equation y=ax+b
			a = (c2-c1) / (v2-v1)
			b = c1 - (a * v1)
			y = a * volt + b
			#print("c1:{} c2:{} v1:{} v2:{} a:{}".format(c1,c2,v1,v2,a))
			return round(y)

		c1 = 0
		v1 = 0
		for c2, v2 in self.d_Cap2V.items():
			if v2 > volt:
				#print("bat approx v:{} vapprox:{} c1:{}".format(volt,approx(c1,c2,v1,v2),c1))
				return approx(c1,c2,v1,v2)
				#return c1 # return the previous value
			c1 = c2
			v1 = v2
		# not found ? raise runtime error?
		return 0


"""
BIC Inverter Device Object:
 - config parameter
 - state of bic
 - charge control
"""
class CBicDevBase():

	e_onl_mode_offline	= 0	# offline not reachable
	e_onl_mode_init		= 1 # init , init can bus, read version...
	e_onl_mode_idle		= 2 # idle mode
	e_onl_mode_running	= 3 # charging/discharging

	s_onl_mode = ['offline','init','idle','running']

	def __init__(self,id : int,type : str):
		self.id = id	# device-id from ini
		if type is None or len(type)==0:
			self.type = "BIC22XX-XXBASE"
		else:
			self.type = type

		self.bic = None
		self.onl_mode = CBicDevBase.e_onl_mode_offline
		self.system_voltage = 0 # needed for power calculation
		self.top_inv = "" # MQTT_T_APP + '/inv/' + str(self.id)
		self.cc = None	# charge control
		self.bat = self.bat = CBattery(self.id) # battery

		self.info = {}
		self.info['id'] = int(self.id) # append some info from bic dump

		self.state = {}
		self.state['onlMode'] = CBicDevBase.s_onl_mode[self.onl_mode]
		self.state['opMode'] = 0  # device operating mode

		self.state['tempC'] = -278
		self.state['acGridV'] = 0 # grid-volatge [V]
		self.state['dcBatV'] = 0 # bat voltage DV [V]
		self.state['capBatPc'] = 0 # bat capacity [%]  @todo

		self.avg_pow_charge = CMAvg(24*3600*1000) #average calculation or charged kWh
		self.avg_pow_discharge = CMAvg(24*3600*1000) #average calculation or dischrged kWh
		self.charge = {}
		self.charge['chargeA'] = 0  # [A] discharge[-] charge[+]
		self.charge['chargeP'] = 0  # [VA] discharge[-] charge[+]
		self.charge['chargeSetA'] = 0 # [A] configured and readed value [A]
		self.charge['chargedKWh'] = 0 # charged kWh
		self.charge['dischargedKWh'] = 0 # discharged kWh
		self.charge_pow_set = 0 # last setter of charge value from mqtt

		self.fault = {} # dic of all fault-states

		self.can_bit_rate = 0 # canbus baud-rate
		self.can_adr = 0 # can address
		self.can_chan_id = "can0" # can channel-id
		self.cfg_max_vcharge100 = 0
		self.cfg_min_vdischarge100 = 6000
		self.cfg_max_ccharge100 = 0 # need to overwrite
		self.cfg_max_cdischarge100 = 0 # need to overwrite

		# min possible value for the BIC-Hardware
		self.cfg_min_ccharge100 = 90  # 0.9[A]
		self.cfg_min_cdischarge100 = 90 # 0.9[A]

		self.tmo_info_ms = 0 #timeslice update info
		self.cfg_tmo_info_ms = 4000 #timeslice update info
		self.tmo_state_ms =  0 #timeslice update state
		self.cfg_tmo_state_ms = 2000 #timeslice update state
		self.tmo_charge_ms =  0 #timeslice update state
		self.cfg_tmo_charge_ms = 2000 #timeslice update state


	def stop(self):
		lg.warning("device stoped id:" + str(self.id))
		self.charge_set_idle()
		#self.bic.operation(0)
		self.onl_mode = CBicDevBase.e_onl_mode_offline
		self.update_state()

	# read from bic some common stuff
	# 	@topic-pub <main-app>/inv/<id>/info
	def	update_info(self):
		dinf=self.bic.dump()
		self.info.update(dinf)
		#lg.info(str(self.info))

		jpl = json.dumps(self.info, sort_keys=False, indent=4)
		global mqttc
		mqttc.publish(MQTT_T_APP + '/inv/' + str(self.id) +  '/info',jpl,0,True) # retained

	""" not used
	def update_power(self):
		if self.onl_mode > CBicDevBase.e_onl_mode_init:
			volt = round(float(self.bic.vread()) / 100,2)
			amp = round(float(self.bic.cread()) / 100,2)
			pow =  round(amp * volt)
			if pow >0:
				self.avg_pow_charge.push_val(pow)
			elif pow <0:
				self.avg_pow_discharge.push_val(pow)
	"""


	# read from bic the voltage and battery parameter
 	# @topic-pub <main-app>/inv/<id>/state
	def update_state(self):

		temp_c= self.bic.tempread()
		if temp_c is not None:
			self.state['tempC'] = int(temp_c / 10)
		else:
			self.state['tempC'] = -278

		op_mode = self.bic.operation_read()
		if op_mode is None:
			self.state['opMode'] = 0
			self.onl_mode =  CBicDevBase.e_onl_mode_offline
			self.state['onlMode'] = 0 # offline, read error
		else:
			self.state['opMode'] = op_mode

		self.state['onlMode'] = CBicDevBase.s_onl_mode[self.onl_mode]

		#print(str(self.state))

		if self.onl_mode > CBicDevBase.e_onl_mode_init:
			volt = round(float(self.bic.vread()) / 100,2)
			amp = round(float(self.bic.cread()) / 100,2)
			ac_grid = round(float(self.bic.acvread()) / 10,0)

			self.state['acGridV'] = ac_grid	# grid-volatge [V]
			self.state['dcBatV'] = volt 	# bat voltage DV [V]
			self.state['capBatPc'] = self.bat.get_capacity_pc(volt)  	# bat capacity [%] , attach CBattery object
		else:
			self.state['acGridV'] = 0
			self.state['dcBatV'] = 0

		jpl = json.dumps(self.state, sort_keys=False, indent=4)
		global mqttc
		mqttc.publish(MQTT_T_APP + '/inv/' + str(self.id) +  '/state',jpl,0,True) # retained


	"""	read from bic the charging/discharging parameter
 		@topic-pub <main-app>/inv/<id>/charge
	"""
	def update_charge(self):
		if self.onl_mode > CBicDevBase.e_onl_mode_init:

			volt = round(float(self.bic.vread()) / 100,2)
			amp = round(float(self.bic.cread()) / 100,2)
			self.state['dcBatV'] = round(volt,1) 	# bat voltage DV [V]
			self.charge['chargeA'] = round(amp,1)  	# bat [A] discharge[-] charge[+] ?
			pow_w = round(amp * volt)
			self.charge['chargeP'] = pow_w  # bat [VA] discharge[-] charge[+]
			cdir = self.bic.BIC_chargemode_read()
			if cdir == CBic.e_charge_mode_charge:
				amp = round((self.bic.charge_current(CBic.e_cmd_read) / 100),2)
				self.avg_pow_charge.push_val(pow_w)
				# W/ms -> kW/h
				self.charge['chargedKWh'] = round(self.avg_pow_charge.sum_get(0,0)/(1E6*3600),1)
			else:
				self.avg_pow_discharge.push_val(pow_w)
				self.charge['dischargedKWh'] = round(self.avg_pow_discharge.sum_get(0,0) / (1E6*3600),1)
				amp = round((self.bic.discharge_current(CBic.e_cmd_read) / 100) * (-1),2)

			self.charge['chargeSetA'] = amp # [A] configured and readed value [A]
		else:
			#self.state['dcBatV'] = 0
			self.charge['chargeA'] = 0
			self.charge['chargeP'] = 0
			self.charge['chargeSetA'] = 0

		jpl = json.dumps(self.charge, sort_keys=False, indent=4)
		global mqttc
		#print('uc' + str(jpl))
		topic = MQTT_T_APP + '/inv/' + str(self.id) +  '/charge'
		mqttc.publish(topic,jpl,0,False) # not retained


	""" ini file config parameter
		@param dbkey-int [DEVICE]Id/X/ChargeVoltage def:2750 volt*100
		@param dbkey-int [DEVICE]Id/X/DischargeVoltage def:2520 volt*100
		@param dbkey-int [DEVICE]Id/X/MaxChargeCurrent def:3500 volt*100
		@param dbkey-int [DEVICE]Id/X/MaxDischargeCurrent def:2600 volt*100
		@topic-sub <main-app>/inv/<id>/state/set [1,0] inverter operating mode
	"""
	def cfg(self,ini):
		lg.info('cfg id:' + str(self.id))
		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		self.cfg_max_vcharge100 = ini.get_int('DEVICE',kpfx("ChargeVoltage"),self.cfg_max_vcharge100)
		self.cfg_min_vdischarge100 = ini.get_int('DEVICE',kpfx("DischargeVoltage") ,self.cfg_min_vdischarge100)

		self.cfg_max_ccharge100 = ini.get_int('DEVICE',kpfx('MaxChargeCurrent'),self.cfg_max_ccharge100)
		self.cfg_max_cdischarge100 = ini.get_int('DEVICE',kpfx('MaxDischargeCurrent'),self.cfg_max_cdischarge100)
		self.top_inv = MQTT_T_APP + '/inv/' + str(self.id)

		self.bat.cfg(ini)

		lg.info("init " + str(self))
		#dischargedelay = int(config.get('Settings', 'DischargeDelay'))


	def __str__(self):
		return "dev id:{} cfg-cv:{} cfg-dv:{} cc:{} cfg-dc:{}".format(self.id,self.cfg_max_vcharge100,self.cfg_min_vdischarge100,self.cfg_max_ccharge100,self.cfg_max_cdischarge100)

	def start(self):
		CBic.can_up(self.can_chan_id,250000)
		self.bic = CBic(self.can_chan_id,self.can_adr)
		ret = self.bic.statusread()
		self.update_info()
		if ret is None:
			self.onl_mode = CBicDevBase.e_onl_mode_offline
		else:
			lg.info('reached init:' + str(self))
			self.onl_mode = CBicDevBase.e_onl_mode_init
			# set the charge and discharge values of the battery
			self.charge_set_idle()
			self.bic.charge_voltage(CBic.e_cmd_write,self.cfg_max_vcharge100)
			self.bic.discharge_current(CBic.e_cmd_write,self.cfg_min_cdischarge100)
			self.bic.operation(1)

		op_mode = self.bic.operation_read()

		if op_mode is None:
			self.state['opMode'] = 0
			self.state['onlMode'] = 0 # offline, read error
		else:
			self.state['opMode'] = op_mode
			if op_mode ==0:
				self.onl_mode = CBicDevBase.e_onl_mode_idle
			else:
				self.onl_mode = CBicDevBase.e_onl_mode_running

		lg.info('dev id:{} started op:{} onl:{}'.format(self.id,op_mode,self.onl_mode))
		#main_exit()


	# poll values from bic/inverter
	# @topic-pub <main-app>/inv/<id>/fault
	def poll(self,timeslive_ms):

		def fault_check_update(force = False):
			fault_update = self.bic.faultread()
			if fault_update is True or force == True:
				jpl = json.dumps(self.bic.d_fault, sort_keys=False, indent=4)
				global mqttc
				mqttc.publish(MQTT_T_APP + '/inv/' + str(self.id) +  '/fault',jpl,0,True) # retained

		if App.ts_1min == 1:
			fault_check_update(True)

		if App.ts_6sec == 1:
			fault_check_update()
		elif App.ts_6sec == 2:
			pass

		if (App.uptime_min % 60)==0:
			self.update_info()

		if self.tmo_info_ms >=0:
			self.tmo_info_ms -= timeslive_ms
		else:
			self.tmo_info_ms = self.cfg_tmo_info_ms
			#self.update_info()

		if self.tmo_state_ms >=0:
			self.tmo_state_ms -= timeslive_ms
		else:
			self.tmo_state_ms = self.cfg_tmo_state_ms
			self.update_state()

		if self.tmo_charge_ms >=0:
			self.tmo_charge_ms -= timeslive_ms
		else:
			self.tmo_charge_ms = self.cfg_tmo_charge_ms
			self.update_charge()

	""" set a new charge value in [A]
		val >0 charging the bat
		val <0 discharging the bat
	"""
	def charge_set_amp(self,val_amp : float):
		val_amp = round(val_amp,1)
		amp100 = 0
		if self.onl_mode >= CBicDevBase.e_onl_mode_idle:
			try:
				amp100 = int(val_amp * 100)
				lg.info("set charge value to:{}[A]".format(round(val_amp,2)))
				if amp100 >=0:
					if amp100 > self.cfg_max_ccharge100:
						amp100 = self.cfg_max_ccharge100
						lg.warning("max charge reached set charge value to:{}A".format(amp100 / 100))
					elif amp100 < self.cfg_min_ccharge100:
						amp100=self.cfg_min_ccharge100
					self.bic.BIC_chargemode(CBic.e_charge_mode_charge)
					self.bic.charge_current(CBic.e_cmd_write,amp100)
				elif amp100 < 0:
					amp100 = abs(amp100)
					if amp100 > self.cfg_max_cdischarge100:
						amp100 = self.cfg_max_cdischarge100
						lg.warning("max discharge reached set discharge value to:{}A".format(-amp100 / 100))
					elif amp100 < self.cfg_min_cdischarge100:
						amp100=self.cfg_min_cdischarge100
					self.bic.BIC_chargemode(CBic.e_charge_mode_discharge)
					self.bic.discharge_current(CBic.e_cmd_write,amp100)
				return 0
			except Exception as err:
				lg.error("can't set charge value:" + str(err))


		lg.error("can't set charge value:" + str(val_amp))

		return -1


	# set charging to neutral position nearby 0.8A charging
	def charge_set_idle(self):
		self.bic.charge_current(CBic.e_cmd_write,self.cfg_min_ccharge100)
		self.bic.discharge_current(CBic.e_cmd_write,self.cfg_min_cdischarge100)
		self.bic.BIC_chargemode(CBic.e_charge_mode_charge)

	def charge_set_pow(self,val_pow:int):
		if self.charge_pow_set == val_pow:
			return
		self.charge_pow_set = val_pow
		v = self.charge['chargeP']
		if v < 20:
			v = 24 # set before read from bic
		amp = int(val_pow) / round(24,1)  # used normaly the real running voltage @fixme
		#print('calcP:' + str(val_pow) + ' amp:' + str(amp))
		self.charge_set_amp(amp)



# device type 2200-24V CAN
class CBicDev2200_24(CBicDevBase):

	def __init__(self,id : int):
		super().__init__(id,"BIC2200-24CAN")
		self.can_bit_rate = 250000 # canbus bit-rate
		self.can_adr = 0x000C0300 # can address

		self.system_voltage = 24 # needed for power calculation
		self.cfg_vcharge100 = 2750
		self.cfg_vdischarge100 = 2520
		self.cfg_max_charge100 = 1000  # 10[A]
		self.cfg_max_cdischarge100 = 1500 # 15[A]



	# special config
	# @param dbkey-int [DEVICE]Id/X/CanBitrate def:250000
	def cfg(self,ini):
		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		super().cfg(ini)
		if self.id >=0:
			self.can_bit_rate = ini.get_int('DEVICE',kpfx("CanBitrate"),250000)
			return 0
		else:
			return -1

	def start(self):
		super().start()


	def poll(self,timeslive_ms):
		super().poll(timeslive_ms)



# device type 2200-24V CAN
class CBicDev2200_24(CBicDevBase):

	def __init__(self,id : int):
		super().__init__(id,"BIC2200-24CAN")
		self.can_bit_rate = 250000 # canbus bit-rate
		self.can_adr = 0x000C0300 # can address

		self.system_voltage = 24 # needed for power calculation
		self.cfg_vcharge100 = 2750
		self.cfg_vdischarge100 = 2520
		self.cfg_max_charge100 = 1000  # 10[A]
		self.cfg_max_cdischarge100 = 1500 # 15[A]



	# special config
	# @param dbkey-int [DEVICE]Id/X/CanBitrate def:250000
	def cfg(self,ini):
		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		super().cfg(ini)
		if self.id >=0:
			self.can_bit_rate = ini.get_int('DEVICE',kpfx("CanBitrate"),250000)
			return 0
		else:
			return -1

	def start(self):
		super().start()


	def poll(self,timeslive_ms):
		super().poll(timeslive_ms)


"""
BIC control and regulation class
- control the bic charge and discharge function
"""
class CChargeCtrlBase():

	DEF_GRID_TMO_SEC = 120 # seconds to switch off bic-dev if we get no new gid-power values
	DEF_BLOCK_TIME_DISCHARGE = 60

	def __init__(self,dev_bic : CBicDevBase):
		self.enabled = False # enabled
		self.dev_bic = dev_bic # device2 control
		self.id = dev_bic.id
		self.tmo_grid_sec = CChargeCtrlBase.DEF_GRID_TMO_SEC # grid tmo timer
		self.grid_pow = 0 # last grid power value
		self.calc_pow = 0 # calculated power to set
		self.ts_1000ms=0 # timeslice 1000ms [ms]
		self.ts_calc_sec=0 # timeslice for each calculation [s]
		self.ts_calc_cfg=12 # timeslice cfg
		self.ts_1min=0 # timeslive 1min [s]

		self.avg_pow = CMAvg(3600*1000) #average calculation , store values for on hour
		self.charge_pow_offset = 0 # [W] offset power for the calculation, move the zero point of power balance
		self.charge_pow_tol = 10 # [W] don't set new charge value if the running one is nearby
		self.discharge_block_tmo_cfg = CChargeCtrlBase.DEF_BLOCK_TIME_DISCHARGE
		self.t_last_charge = datetime.now()

		self.lst_discharge_block_hour = [-1,-1] # between this two hours , block discharging

	@staticmethod
	def sign(x):
		if x >= 0:
			return 1
		return -1

	@staticmethod
	def clamp(n, minn, maxn):
		return max(min(maxn, n), minn)

	# @return the time diff in seconds for the given time and now
	@staticmethod
	def get_time_diff_sec(t_past):
		now =  datetime.now()
		diff = now - t_past
		return diff.seconds


	def __str__(self):
		ret = "CC-id:{} gp:{}[W] sp:{}[W]".format(self.id,self.grid_pow,self.cal_pow)
		return ret

	"""
	Charge Contol and regulator

	ini file config parameter
	@param dbkey-str [CHARGE_CONTROL]Id/X/TopicPower def:"" topic to subscribe power values from smart meter [W] <0:power to public-grid, >0 power-consumption from public.grid
	@param dbkey-int [CHARGE_CONTROL]Id/X/Enabled def:1 if it is defined  in ini -> enabled
	not used yet @param dbkey-int [CHARGE_CONTROL]Id/X/TimeSliceCalcSec def:12[s] timeslice for each calculation [s]

	@param dbkey-int [CHARGE_CONTROL]Id/X/DischargeBlockTimeSec def: 60[s] skip short discharge bursts
	@param dbkey-int [CHARGE_CONTROL]Id/X/ChargePowerOffset def: 0[W] offset power for the calculation, move the zero point of power balance
	@param dbkey-int [CHARGE_CONTROL]Id/X/ChargeTol def: 10[W] don't set new charge value if the running one is nearby

	@param dbkey-int [CHARGE_CONTROL]Id/X/DischargeBlockHourStart def:-1 [0..23] start interval hour of day to block discharging
	@param dbkey-int [CHARGE_CONTROL]Id/X/DischargeBlockHourStop def:-1  [0..23] stop interval hour of day to block discharging

	"""
	def cfg(self,ini):
		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		top_pow = ini.get_str('CHARGE_CONTROL',kpfx('TopicPower'),MQTT_APP_ID)
		msg = CMQTT.CMSG(top_pow,"dummypl")
		msg.cb = self.cb_mqtt_sub_power
		msg.cb_user_data = self.on_cb_grid_power
		global mqttc
		mqttc.append_subscribe(msg)
		lg.info('CC grid power topic:' + str(top_pow))
		_enabled = ini.get_int('CHARGE_CONTROL',kpfx('Enabled'),0)
		if _enabled >0:
			self.enabled = True
		self.ts_calc_cfg = ini.get_int('CHARGE_CONTROL',kpfx('TimeSliceCalcSec'),self.ts_calc_cfg)

		self.discharge_block_tmo_cfg = ini.get_int('CHARGE_CONTROL',kpfx('DischargeBlockTimeSec'),CChargeCtrlBase.DEF_BLOCK_TIME_DISCHARGE)
		self.charge_pow_offset = ini.get_int('CHARGE_CONTROL',kpfx('ChargePowerOffset'),self.charge_pow_offset)
		self.charge_pow_tol = ini.get_int('CHARGE_CONTROL',kpfx('ChargeTol'),self.charge_pow_tol)
		self.lst_discharge_block_hour[0] = ini.get_int('CHARGE_CONTROL',kpfx('DischargeBlockHourStart'),-1) # invalidate as default
		self.lst_discharge_block_hour[1] = ini.get_int('CHARGE_CONTROL',kpfx('DischargeBlockHourStop'),-1) # invalidate as default
		return

	# @return True if discharging is blocked
	def discharge_blocking_state(self):
		ret = False
		tb = self.lst_discharge_block_hour[0]
		te = self.lst_discharge_block_hour[1]
		if (tb >= 0) and (te >= tb) and (tb < 24) and (te < 24):
			now =  datetime.now()
			if (now.hour >= tb) and (now.hour <= te):
				#print('h:' + str(now.hour))
				lg.info('CC discharge block interval:[{}-{}] h:{}'.format(tb,te,now.hour))
				ret = True

		tdiff = CChargeCtrlBase.get_time_diff_sec(self.t_last_charge)
		if tdiff <= self.discharge_block_tmo_cfg:
			lg.info('CC discharge block time tmo:{}[s]'.format(tdiff))
			ret = True
		return ret

	# set the calculated power
	def calc_power_set(self,val_pow : int):
		self.calc_pow = val_pow
		if val_pow >0:
			self.t_last_charge = datetime.now()


	# calculate new power value to set, overwrite it !
	def calc_power(self,grid_pow):
		if self.dev_bic.onl_mode <= CBicDevBase.e_onl_mode_idle:
			return False

		return True

	def enable(self,enable):
		self.enabled=enable
		lg.info('CC charge control enable:' +str(enable))
		self.dev_bic.charge_set_idle() # reset to lowest charge value
		self.calc_pow = 0

	def poll(self,timeslice_ms):

		self.ts_1000ms += timeslice_ms
		if self.ts_1000ms >= 1000:
			self.ts_1000ms=0

			self.ts_calc_sec+=1
			#print(str(self.ts_calc_sec))
			if self.ts_calc_sec > (self.ts_calc_cfg-1):
				self.ts_calc_sec = 0

			self.ts_1min+=1
			if self.ts_1min >59:
				self.ts_1min = 0

			if self.tmo_grid_sec >= 0:
				self.tmo_grid_sec-1
				if self.tmo_grid_sec < 0:
					self.on_cb_grid_power_tmo()
		return


	# new power value from grid:
	# payload: try to parse a simple value in [W]
	def cb_mqtt_sub_power(self,mqttc,user_data,mqtt_msg):
		try:
			self.grid_pow = int(mqtt_msg.payload)
			self.tmo_grid_sec = CChargeCtrlBase.DEF_GRID_TMO_SEC
			self.on_cb_grid_power(self.grid_pow)
		except ValueError:
			pass

	""" received a new value from the grid power sensor
		power value: >0 receive power from the public-grid
		power value: <0 inject power to the public-grid
	"""
	def on_cb_grid_power(self,pow_val):
		self.tmo_grid_sec = CChargeCtrlBase.DEF_GRID_TMO_SEC
		lg.info('CC (default) new grid power value {}[W]'.format(self.grid_pow))

	# grid power smart meter timeout stop discharging ?
	def on_cb_grid_power_tmo():
		lg.warning('CC grid power TMO')
		return


"""
BIC control and regulation class
- simple one charge and discharge depends on power grid value
"""
class CChargeCtrlSimple(CChargeCtrlBase):

	def __init__(self,dev_bic : CBicDevBase):
		super().__init__(dev_bic)
		self.cfg_loop_gain = 0.5 # regulator loop gain for new values


	""" Charge Control Simple:
	"""
	def cfg(self,ini):

		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		super().cfg(ini)
		self.cfg_loop_gain = round(ini.get_float('CHARGE_CONTROL',kpfx('LoopGain'),self.cfg_loop_gain),1)

	""" received a new value from the grid power sensor
		power value: >0 receive power from the public-grid
		power value: <0 inject power to the public-grid
		usefull functions for the future:
		- haus/kel/pgrid/pnow
	"""
	def on_cb_grid_power(self,pow_val):
		def avg2min(minute : int):
			return self.avg_pow.avg_get(minute*60*1000,-1)

		self.tmo_grid_sec = CChargeCtrlBase.DEF_GRID_TMO_SEC
		#lg.info('CC new grid power value {} [W]'.format(self.grid_pow))
		self.avg_pow.push_val(pow_val)

		lg.info('CC GRID POW:{}[W] AVG:1m:{} 2m:{} 5m:{} 1h:{} offs:{}[W]'.format(pow_val,avg2min(1),avg2min(2),avg2min(5),avg2min(60),self.charge_pow_offset))
		if self.enabled is True:
			self.calc_power(pow_val)



	""" simple charge discharge control:
		- new charge value = grid-power * (-1)
		- discharge block time, skip fast charge, discharge toggle
	"""
	def calc_power(self,grid_pow):

		is_running = super().calc_power(grid_pow)
		if is_running is False:
			return

		charge_pow = self.dev_bic.charge['chargeP']
		grid_pow = self.avg_pow.avg_get(1*1000*60,-1) + self.charge_pow_offset # used avg grid power with offset

		POW_LIMIT = 800

		new_calc_pow = math_round_up(charge_pow  + (grid_pow * self.cfg_loop_gain * (-1)))

		""" testcode simple linear control
		#pdiff = round(grid_pow - self.calc_pow,-1)
		if grid_pow >=0:
			sign = -1
		else:
			sign = 1

		agp = abs(grid_pow)

		if agp <= self.charge_pow_tol:
			new_calc_pow = self.calc_pow
		elif agp <= 100:
			new_calc_pow = self.calc_pow + (10 * sign)
		elif agp <= 200:
			new_calc_pow = self.calc_pow + (20 * sign)
		elif agp <= 400:
                        new_calc_pow = self.calc_pow + (100 * sign)
		else:
			new_calc_pow = self.calc_pow + (200 * sign)

		#print("diff:" + str(agp) + ' cp:' + str(new_calc_pow) + ' sign:' + str(sign))
		"""

		if new_calc_pow > 0 and new_calc_pow > POW_LIMIT:
			new_calc_pow = POW_LIMIT
		elif new_calc_pow < 0 and abs(new_calc_pow) > POW_LIMIT:
			new_calc_pow = -POW_LIMIT

		# check and skip short discharge burst e.g. use the grid power for the tee-kettle

		if new_calc_pow < 0:
			if self.discharge_blocking_state() is True:
				new_calc_pow = 0

		print('d:{} tol:{}'.format((charge_pow - new_calc_pow),self.charge_pow_tol))
		if abs(int(charge_pow - new_calc_pow)) > self.charge_pow_tol:
			lg.info('CC set new value: grid:{} now:{}[W] calc:{}[W] ofs:{}[W]'.format(grid_pow,charge_pow,new_calc_pow,self.charge_pow_offset))
			topic = self.dev_bic.top_inv + '/charge/set'
			dpl = {"var":"chargeP"}
			dpl['val'] = int(new_calc_pow)
			#print("top:{} pl:{}".format(topic,str(dpl)))
			global mqttc
			mqttc.publish(topic,json.dumps(dpl, sort_keys=False, indent=0),0,False) # no retain
		else:
			lg.info('CC const value: grid:{} now:{}[W] calc:{}[W] ofs:{}[W]'.format(grid_pow,charge_pow,new_calc_pow,self.charge_pow_offset))

		self.calc_power_set(new_calc_pow)
		return


	def poll(self,timeslice_ms):
		super().poll(timeslice_ms)


# simple pid regulator
class CPID :

	def __init__(self):
		self.cfg_dt  = 1 	# time-between steps
		self.cfg_offset = 0 # const-offset
		self.cfg_min = 0		# min allow value
		self.cfg_max = 0		# max allow value
		self.cfg_kp  = 1		# K-Part gain
		self.cfg_ki  = 0		# I-Part gain set 0 for simple K-regulator
		self.cfg_kd  = 0		# D-Part gain set 0 for simple K-regulator

		self.err = 0.0			# last error values step(t-1)
		self.I_val = 0.0		# I-part Value


	# configuration and reset of the pid
	def cfg(self, dt_sec,offset, vmin, vmax, kp, ki, kd):
		def rnd(val):
			return round(val,1)

		self.err = 0.0
		self.I_val = 0.0
		self.cfg_dt  = int(dt_sec) 	# time-between steps
		self.cfg_offset = float(offset) # const-offset
		self.cfg_min = float(vmin)	# min allow value
		self.cfg_max = float(vmax)	# max allow value
		self.cfg_kp  = float(kp)	# K-Part gain
		self.cfg_ki  = float(ki)	# I-Part gain set 0 for simple K-regulator
		self.cfg_kd  = float(kd)	# D-Part gain set 0 for simple K-regulator
		lg.info("pid cfg: P:{} I:{} D:{} offset:{} dt:{}".format(rnd(kp),rnd(ki),rnd(kd),rnd(offset),self.cfg_dt))
		return


	# calculate next pid step after dt
	def step(self,act_val) :

		def clamp(n, minn, maxn):
			return max(min(maxn, n), minn)

		_err = self.cfg_offset - act_val
		P = self.cfg_kp * _err

		self.I_val += _err * self.cfg_dt
		I = self.cfg_ki * self.I_val

		D = self.cfg_kd * (_err - self.err) / self.cfg_dt

		ret_val = P + I + D
		ret_val=clamp(ret_val,self.cfg_min,self.cfg_max)

		self.err = _err
		lg.debug("pid stp v:{} p:{} i:{} d:{} err:{} ret:{}".format(act_val,P,I,D,_err,ret_val))
		return int(ret_val)

"""
 BIC control and regulation class
- pid controlled

"""
class CChargeCtrlPID(CChargeCtrlBase):

	def __init__(self,dev_bic : CBicDevBase):
		super().__init__(dev_bic)
		self.pid = CPID() # pid regulator
		self.cfg_loop_gain = 0.5 # regulator loop gain for new values


	""" Charge Control Simple:
		@param dbkey-int [CHARGE_CONTROL]Id/X/Pid/ClockSec def:0
		@param dbkey-int [CHARGE_CONTROL]Id/X/Pid/Min def:0
		@param dbkey-int [CHARGE_CONTROL]Id/X/Pid/Max def:0
		@param dbkey-float [CHARGE_CONTROL]Id/X/Pid/P def:1
		@param dbkey-float [CHARGE_CONTROL]Id/X/Pid/I def:0
		@param dbkey-float [CHARGE_CONTROL]Id/X/Pid/D def:0
		"""
	def cfg(self,ini):

		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		super().cfg(ini)
		self.pid.cfg(
			ini.get_int('CHARGE_CONTROL',kpfx('Pid/ClockSec'),1),
			self.charge_power_offset,
			ini.get_int('CHARGE_CONTROL',kpfx('Pid/Min'),0),
			ini.get_int('CHARGE_CONTROL',kpfx('Pid/Max'),0),
			ini.get_float('CHARGE_CONTROL',kpfx('Pid/P'),1),
			ini.get_float('CHARGE_CONTROL',kpfx('Pid/I'),0),
			ini.get_float('CHARGE_CONTROL',kpfx('Pid/D'),0)
		)
		return

	""" received a new value from the grid power sensor
		power value: >0 receive power from the public-grid
		power value: <0 inject power to the public-grid
		usefull functions for the future:
		- haus/kel/pgrid/pnow
	"""
	def on_cb_grid_power(self,pow_val):
		def avg2min(minute : int):
			return self.avg_pow.avg_get(minute*60*1000,-1)

		self.tmo_grid_sec = CChargeCtrlBase.DEF_GRID_TMO_SEC
		#lg.info('CC new grid power value {} [W]'.format(self.grid_pow))
		self.avg_pow.push_val(pow_val)

		lg.info('CC GRID POW:{}[W] AVG:1m:{} 2m:{} 5m:{} 1h:{} offs:{}[W]'.format(pow_val,avg2min(1),avg2min(2),avg2min(5),avg2min(60),self.charge_pow_offset))
		if self.enabled is True:
			self.calc_power(pow_val)



	""" charge discharge control via PID
		- discharge block time, skip fast charge, discharge toggle
		- discharge only over night if configured
		PID Settings:

	"""
	def calc_power(self,grid_pow):

		is_running = super().calc_power(grid_pow)
		if is_running is False:
			return

		charge_pow = self.dev_bic.charge['chargeP'] # charge power of the bat from real voltage and current of the bic
		grid_pow = self.avg_pow.avg_get(1*1000*60,-1) + self.charge_pow_offset # used avg grid power with offset

		new_calc_pow = self.pid.step(grid_pow)

		# check and skip short discharge burst e.g. use the grid power for the tee-kettle

		if new_calc_pow < 0:
			if self.discharge_blocking_state() is True:
				new_calc_pow = 0

		if abs(int(charge_pow - new_calc_pow)) > self.charge_pow_tol:
			lg.info('CC set new value: grid:{} now:{}[W] calc:{}[W] ofs:{}[W]'.format(grid_pow,charge_pow,new_calc_pow,self.charge_pow_offset))
			topic = self.dev_bic.top_inv + '/charge/set'
			dpl = {"var":"chargeP"}
			dpl['val'] = int(new_calc_pow)
			#print("top:{} pl:{}".format(topic,str(dpl)))
			global mqttc
			mqttc.publish(topic,json.dumps(dpl, sort_keys=False, indent=0),0,False) # no retain
		else:
			lg.info('CC const value: grid:{} now:{}[W] calc:{}[W] ofs:{}[W]'.format(grid_pow,charge_pow,new_calc_pow,self.charge_pow_offset))

		self.calc_power_set(new_calc_pow)
		return


	def poll(self,timeslice_ms):
		super().poll(timeslice_ms)


"""
Main App
 - poll some objects in a timeslice
 - receive subscribed toppics
 - publish some charge infos from the inverter device
"""
class App:
	ts_1000ms=0 # [ms]
	ts_6sec=0   # [s]
	ts_1min=0   # [s]
	uptime_min=0

	def __init__(self,cmqtt):
		self.cmqtt = cmqtt
		#self.ini = ini
		#self.id= ini.get_str('MQTT','AppId',MQTT_APP_ID)
		self.t_start =  datetime.now()   # time.localtime()
		self.info = {}
		self.started = False
		self.dev_bic = {} # all bic hardware devices
		self.bat = CBattery(0)

	def stop(self):
		for dev in self.dev_bic.values():
			dev.stop()

	""" BIC Config
		[DEVICE]
		@param dbkey-str [DEVICE]Id/X/Type def:empty well known modem type "BIC2200"
		@param dbkey-int [DEVICE]Id/X/CanBaudRate def:0 Baudrate
		@topic-sub <main-app>/inv/<id>/charge/set {"var":[chargeA,chargeP],"val":[ampere or power]]}
	"""
	def cfg(self,ini):
		# @future-use iterate over all bic's
		id = 0
		dev_type = ini.get_str('DEVICE','Id/{}/Type'.format(id),"")
		self.bat.cfg(ini)
		if dev_type == 'BIC2200-24CAN':
			dev = CBicDev2200_24(id)
			dev.cfg(ini)
			self.dev_bic[id] = dev
			if len(self.dev_bic) >0:
				lst_sub = ['charge/set','state/set','control/set']
				for sub in lst_sub:
					msg = CMQTT.CMSG(dev.top_inv + "/" + sub,"dummypl")
					msg.cb = self.cb_mqtt_sub_event
					msg.cb_user_data = dev
					mqttc.append_subscribe(msg)
				"""
				msg = CMQTT.CMSG(dev.top_inv + "/charge/set","dummypl")
				msg.cb = self.cb_mqtt_sub_event
				msg.cb_user_data = dev
				mqttc.append_subscribe(msg)
				msg = CMQTT.CMSG(dev.top_inv + "/state/set","dummypl")
				msg.cb = self.cb_mqtt_sub_event
				msg.cb_user_data = dev
				mqttc.append_subscribe(msg)
				msg = CMQTT.CMSG(dev.top_inv + "/control/set","dummypl")
				msg.cb = self.cb_mqtt_sub_event
				msg.cb_user_data = dev
				mqttc.append_subscribe(msg)
				"""
			dev.cc = CChargeCtrlSimple(dev)
			dev.cc.cfg(ini)


	""" set charging parameter
		@topic-sub <main-app>/inv/<id>/charge/set {"var":[chargeA,chargeP],"val":[ampere or power]}
		@topic-sub <main-app>/inv/<id>/control/set [0,1] start stop charge-control
	"""
	def cb_mqtt_sub_event(self,mqttc,user_data,mqtt_msg):
		#print('on subsc:' + mqtt_msg.pp())
		dev = user_data
		if dev.top_inv + "/charge/set" == mqtt_msg.topic:
			try:
				dpl = json.loads(mqtt_msg.payload)
				if 'var' in dpl and 'val' in dpl:
					if dpl['var'] == 'chargeA':
						dev.charge_set_amp(dpl['val'])
					elif dpl['var'] == 'chargeP':
						dev.charge_set_pow(dpl['val'])
			except:
				pass
		elif dev.top_inv + "/state/set" == mqtt_msg.topic:
			if mqtt_msg.payload == '1':
				dev.op_mode = dev.bic.operation(1) # on
			elif mqtt_msg.payload == '2':
				dev.op_mode = dev.bic.operation(2) # toggle
			else:
				dev.op_mode = dev.bic.operation(0)
			dev.state['opMode'] = dev.op_mode
			lg.info('set operation mode:' + str(dev.op_mode))
			if dev.op_mode > 0:
				dev.onl_mode = CBicDevBase.e_onl_mode_running
			else:
				dev.onl_mode = CBicDevBase.e_onl_mode_idle
				dev.charge_set_idle()	# off
		elif dev.top_inv + "/control/set" == mqtt_msg.topic:
			if mqtt_msg.payload == '1':
				dev.cc.enable(True)
			else:
				dev.cc.enable(False)

	def start(self):
		if self.started is False:
			self.started=True
			#self.mqttc.publish(MQTT_T_APP + '/state/devall',json.dumps(lst_dev_cfg, sort_keys=False, indent=4),0,True) # retained
			for dev in self.dev_bic.values():
				dev.start()

	def poll(self,timeslice_ms):
		if self.started is False:
				pass

		App.ts_1000ms+=timeslice_ms
		if App.ts_1000ms > 1000:
			App.ts_1000ms=0
			for dev in self.dev_bic.values():
				dev.poll(1000)
				if dev.cc is not None:
					dev.cc.poll(1000)

			App.ts_6sec+=1
			if App.ts_6sec >5:
				App.ts_6sec=0

			App.ts_1min+=1
			if App.ts_1min >59:
				App.ts_1min=0
				App.uptime_min+=1
				mqttc.publish(MQTT_T_APP,self.json_encode(),0,True)

	def json_encode(self):
		self.info['appVer'] = APP_VER
		self.info['appName'] = APP_NAME
		self.info['startTS'] =  self.t_start.strftime('%y%m%d_%H:%M:%S')   #  time.strftime("%y%m%d_%H:%M:%S",self.t_start)
		self.info['ts'] = datetime.now().strftime('%y%m%d_%H:%M:%S.%f')[:-3]
		self.info['conTimeMin'] = App.uptime_min
		self.info['conCnt'] = mqttc.conn_cnt
		return json.dumps(self.info, sort_keys=False, indent=4)

# roundup 56->10
def math_round_up(val):
	return int(round(val,-1))

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

	logging.getLogger('can').setLevel(logging.INFO)

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
	lg.warning("main exit reached")
	mqttc.stop()

	global app
	if app is not None:
		app.stop()

	exit(0)

if __name__ == "__main__":
	main_init()
	if mqttc is not None and len(mqttc.app_ip_adr):
		try:
			mqttc.connect(str(mqttc.app_ip_adr))
		except:
			logging.info("mqtt\tcan't connect to broker:" + MQTT_BROKER_ADR)

	poll_time_slice_ms=20
	poll_time_slice_sec=poll_time_slice_ms/1000 # 20ms
	while True:
		app.poll(poll_time_slice_ms)
		try:
			time.sleep(poll_time_slice_sec)
		except KeyboardInterrupt:
			main_exit()


