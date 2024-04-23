#!/usr/bin/env python3
APP_VER = "0.20"
APP_NAME = "bic2mqtt"

"""
 fst:05.04.2024 lst:23.04.2024
 Meanwell BIC2200-XXCAN to mqtt bridge
 V0.20 ..charge control testing
 V0.10 charge and discharging is possible for device BIC2200-24-CAN
 V0.04 cbic2200 first tests
 V0.01 mqtt is running
 V0.00 No fuction yet, working on the app-frame


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
			b = c1
			y = a * volt + b
			return round(y)

		c1 = 0
		v1 = 0
		for c2, v2 in self.d_Cap2V.items():
			if v2 > volt:
				#print("bat approx v:{} vapprox:{} c1:{}".format(volt,approx(c1,c2,v1,v2),c1))
				return c1 # return the previous value
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

		self.charge = {}
		self.charge['chargeA'] = 0  # [A] discharge[-] charge[+]
		self.charge['chargeP'] = 0  # [VA] discharge[-] charge[+]
		self.charge['chargeSetA'] = 0 # [A] configured and readed value [A]


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
		self.bic.charge_current(CBic.e_cmd_write,self.cfg_min_ccharge100)
		self.bic.discharge_current(CBic.e_cmd_write,self.cfg_min_cdischarge100)
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
			self.state['dcBatV'] = volt 	# bat voltage DV [V]
			self.charge['chargeA'] = amp  	# bat [A] discharge[-] charge[+] ?
			self.charge['chargeP'] = round(amp * volt)  # bat [VA] discharge[-] charge[+]
			cdir = self.bic.BIC_chargemode_read()
			if cdir == CBic.e_charge_mode_charge:
				amp = round((self.bic.charge_current(CBic.e_cmd_read) / 100),2)
			else:
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
		@topic-sub <main-app>/inv/<id>/state/set [1,0] inverter operating mode @todo
	"""
	def cfg(self,ini):
		lg.info('cfg id:' + str(self.id))
		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		self.cfg_max_vcharge100 = ini.get_int('DEVICE',kpfx("ChargeVoltage"),self.cfg_max_vcharge100)
		self.cfg_min_vdischarge100 = ini.get_int('DEVICE',kpfx("DischargeVoltage") ,self.cfg_min_vdischarge100)

		self.cfg_max_ccharge100 = 1000 #ini.get_int('DEVICE',kpfx(MaxChargeCurrent),self.cfg_max_ccharge100)
		self.cfg_max_cdischarge100 = 1000 #ini.get_int('DEVICE',kpfx(MaxDischargeCurrent),self.cfg_max_cdischarge100)
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
			self.bic.charge_voltage(CBic.e_cmd_write,self.cfg_max_vcharge100)
			self.bic.discharge_voltage(CBic.e_cmd_write,self.cfg_min_vdischarge100)
			self.bic.charge_current(CBic.e_cmd_write,self.cfg_min_ccharge100)
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
		amp100 = 0
		if self.onl_mode >= CBicDevBase.e_onl_mode_idle:
			try:
				amp100 = int(val_amp * 100)
				lg.info("set charge value to:{}A  A100({})".format(round(val_amp,2),amp100))
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
			except:
				pass

		lg.error("can't set charge value:" + str(val_amp))

		return -1

	def charge_set_pow(self,val_pow:int):
		v = self.charge['chargeP']
		if v < 20:
			v = 24 # set before read from bic
		amp = int(val_pow) / v
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



""" @todo
BIC control and regulation class
- control the bic charge and discharge function
"""
class CChargeCtrlBase():

	DEF_GRID_TMO_SEC = 18 # seconds to switch off bic-dev if we get no new gid-power values

	def __init__(self,dev_bic : CBicDevBase):
		self.enabled = False # enabled
		self.dev_bic = dev_bic # device2 control
		self.id = dev_bic.id
		self.tmo_grid_sec = CChargeCtrlBase.DEF_GRID_TMO_SEC # grid tmo timer
		self.grid_pow = 0 # last grid power value
		self.calc_pow = 0 # calculated power to set 
		self.ts_1000ms=0 # timeslice 1000ms [ms]
		self.ts_6sec=0 # timeslive 6sec [s]
		self.ts_1min=0 # timeslive 1min [s]
		self.new_grid_power_value = False # grid power received value arrived

	def __str__(self):
		ret = "cc-id:{} gp:{}[W] sp:{}[W]".format(self.id,self.grid_pow,self.cal_pow)
		return ret

	""" @todo
	Charge Contol and regulator

	ini file config parameter
	@param dbkey-str [CHARGE_CONTROL]Id/X/TopicPower def:"" topic to subscribe power values from smart meter [W] <0:power to public-grid, >0 power-consumption from public.grid
	@param dbkey-int [CHARGE_CONTROL]Id/X/Enabled def:1 if it is defined  in ini -> enabled
	# not used
	@param dbkey-int [CHARGE_CONTROL]Id/X/Type def:"SIMPLE" if the type is undefined, the regulation is disabled"
	@param dbkey-int [CHARGE_CONTROL]Id/X/NightCap def:30 store capacity for the night [%]
	@param dbkey-int [CHARGE_CONTROL]Id/X/NightStartTime def:18:00 start night mode at HH:MM, allow discharging NightCap
	@param dbkey-int [CHARGE_CONTROL]Id/X/GridPowerDischargeMin def: 50 Discharge min. power [W]
	@param dbkey-int [CHARGE_CONTROL]Id/X/GridPowerChargeMin  def:-40 Start Charging if the grid power is smaller than this value [W]

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
		return


	# calculate new power value to set, overwrite it !
	def calc_power(self):
		#self.calc_power = 0 

	def poll(self,timeslice_ms):

		self.ts_1000ms += timeslice_ms
		if self.ts_1000ms >= 1000:
			self.ts_1000ms=0

			self.ts_6sec+=1
			if self.ts_6sec >6:
				self.ts_6sec = 0

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
	# @todo add a simple json parser
	def cb_mqtt_sub_power(self,mqttc,user_data,mqtt_msg):
		try:
			self.grid_pow = int(mqtt_msg.payload)
			self.tmo_grid_sec = CChargeCtrlBase.DEF_GRID_TMO_SEC
			self.on_cb_grid_power(self.grid_pow)
			if self.enabled is True:
				self.new_grid_power_value = True
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


""" @todo
BIC control and regulation class
- simple one charge and discharge depends on power grid value
"""
class CChargeCtrlSimple(CChargeCtrlBase):

	DEF_BLOCK_TIME_DISCHARGE = 60

	def __init__(self,dev_bic : CBicDevBase):
		super().__init__(dev_bic)
		self.avg_pow = CMAvg(3600*1000) #average calculation , store values for on hour
		self.charge_pow_offset = 0 # [W] offset power for the calculation, move the zero point of power balance
		self.charge_pow_tol = 10 # [W] don't set new charge value if the running one is nearby
		self.discharge_block_tmo = 0 # [s]  # skip short discharge burst
		self.discharge_block_tmo_cfg = CChargeCtrlSimple.DEF_BLOCK_TIME_DISCHARGE


	""" Charge Control Simple:
		@param dbkey-int [CHARGE_CONTROL]Id/X/DischargeBlockTimeSec def: 60[s] skip short discharge bursts
		@param dbkey-int [CHARGE_CONTROL]Id/X/ChargePowerOffset def: 0[W] offset power for the calculation, move the zero point of power balance
		@param dbkey-int [CHARGE_CONTROL]Id/X/ChargeTol def: 10[W] don't set new charge value if the running one is nearby
	"""
	def cfg(self,ini):
		super().cfg(ini)

		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		self.discharge_block_tmo_cfg = ini.get_str('CHARGE_CONTROL',kpfx('DischargeBlockTimeSec'),CChargeCtrlSimple.DEF_BLOCK_TIME_DISCHARGE)
		self.charge_pow_offset = ini.get_str('CHARGE_CONTROL',kpfx('ChargePowerOffset'),self.charge_pow_offset)
		self.charge_pow_tol = ini.get_str('CHARGE_CONTROL',kpfx('ChargeTol'),self.charge_pow_tol)

	""" received a new value from the grid power sensor
		power value: >0 receive power from the public-grid
		power value: <0 inject power to the public-grid
		usefull functions for the future:
		- haus/kel/pgrid/pnow
	"""
	def on_cb_grid_power(self,pow_val):
		self.tmo_grid_sec = CChargeCtrlBase.DEF_GRID_TMO_SEC
		lg.info('CC new grid power value {} [W]'.format(self.grid_pow))
		self.avg_pow.push_val(pow_val)
		lg.info('CC val:{}[W] 1min:{}[W] 5min:{}[W] 1h:{}[W]'.format(pow_val,self.avg_pow.avg_get(60*1000),self.avg_pow.avg_get(5*60*1000),self.avg_pow.avg_get(60*60*1000)))


	""" simple charge discharge control:
		- new charge value = grid-power * (-1)
		- discharge block time, skip fast charge, discharge toggle
	"""
	def calc_power(self):
		#self.calc_power = 0 
		charge_pow = self.dev_bic.charge['chargeP']
		grid_pow = self.avg_pow.avg_get(60*1000) + self.charge_pow_offset
		
		new_calc_pow = self.calc_pow + grid_pow * (-1)

		# check and skip short discharge burst e.g. use the grid power for the tee-kettle
		if new_calc_pow >0:
			self.discharge_block_tmo = self.discharge_block_tmo_cfg
		elif new_calc_pow <0 and self.discharge_block_tmo >=0:
			self.discharge_block_tmo -= 6
			lg.info('cc discharge block time now:{}[W] calc:{}[W] tmo:{}[s]'.format(charge_pow,new_calc_pow,self.discharge_block_tmo))
			return

		if abs(self.calc_power - new_calc_pow) > self.charge_pow_tol:
			lg.info('cc set new value: now:{}[W] calc:{}[W]'.format(charge_pow,new_calc_pow))
			topic = self.dev_bic.top_inv + '/charge/set'
			dpl = {"var":"chargeP"}
			dpl['val'] = int(new_calc_pow)
			print("top:{} pl:{}".format(topic,str(dpl)))
			global mqttc
			#mqttc.publish(topic,json.dumps(dpl, sort_keys=False, indent=0),0,False) # no retain

		self.calc_power = new_calc_pow


	def poll(self,timeslice_ms):
		super().poll(timeslice_ms)
		if self.ts_6sec == 5 and self.new_grid_power_value is True:
			self.new_grid_power_value = False
			self.calc_power()

	def cfg(self,ini):
		super().cfg(ini)
		pass

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
				msg = CMQTT.CMSG(dev.top_inv + "/charge/set","dummypl")
				msg.cb = self.cb_mqtt_sub_event
				msg.cb_user_data = dev
				mqttc.append_subscribe(msg)
				msg = CMQTT.CMSG(dev.top_inv + "/state/set","dummypl")
				msg.cb = self.cb_mqtt_sub_event
				msg.cb_user_data = dev
				mqttc.append_subscribe(msg)

			dev.cc = CChargeCtrlSimple(dev)
			dev.cc.cfg(ini)


	""" set charging parameter
		@topic-sub <main-app>/inv/<id>/charge/set {"var":[chargeA,chargeP],"val":[ampere or power]}
	"""
	def cb_mqtt_sub_event(self,mqttc,user_data,mqtt_msg):
		print('on subsc:' + mqtt_msg.pp())
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
				dev.bic.operation(1)
			else:
				dev.bic.operation(0)


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


