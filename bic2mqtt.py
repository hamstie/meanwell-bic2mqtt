#!/usr/bin/env python3
APP_VER = "0.91"
APP_NAME = "bic2mqtt"

"""
 fst:05.04.2024 lst:23.07.2024
 Meanwell BIC2200-XXCAN to mqtt bridge
 V0.91 ...Charge control for the winter
 V0.81 released-surplus, used the grid power as power value
 V0.75 -grid offset as profile value
 V0.74 -Bugfix charge reset sign detection and value type
 V0.72 -exit on startup if bic dev access failed
 V0.71 -catch CAN write exception in reset function
 V0.70 ...surplusP,surplusKWh calculation
 V0.64 -try2decrease eeprom writes if the bat is low or full
 V0.62 -parse program argument: ini file path and name
       -move charge min/max parameter from pid to base class
 V0.61 +charge profiles for each hour
 V0.53 ..fast pid reset if grid power changed the direction
 V0.52 -minimize eeprom write access (cfg tolerance & rounding)
	   -audit grid-power tmo handling
 V0.51 -pid-charge control is running
 ....

 @todo:
	P4: (Toggling display string for MQTT-Dashboards: Power,Temp,Voltage..)
	P2: Charge control for the winter


 new feature:
 	- publish mqtt topic if the bat-charge power and set power diff reached threshold (bat is full, start some consumer)

 - EEPROM Write disable is possible since bic-firmware datecode:2402???

"""

import logging
#future use from logging.handlers import RotatingFileHandler

from cmqtt import CMQTT
from cbic2200 import CBic

from datetime import datetime
import time
import os.path
import json
import configparser
from cavg import CMAvg

import sys

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

	def reload(self):
		lg.info('ini config reload file:' + str(self.fname))
		lret = self.cfg.read(self.fname)
		if len(lret) == 0:
			raise FileNotFoundError(self.fname)

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
	def cfg(self,ini,reload = False):
		d = ini.get_sec_keys('BAT_0')
		for k,v in d.items():
				if k.find('cap2v/')>=0:
					cap_pc = int(k.replace('cap2v/',''))
					self.d_Cap2V[cap_pc] = float(v)
		self.check()

	# @return the capacity of the battery [%]
	def get_capacity_pc(self,volt):

		#approx values between two cap values in the list
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
		self.state['capBatPc'] = 0 # bat capacity [%]

		self.avg_pow_charge = CMAvg(24*3600*1000) #average calculation fr charged [kWh]
		self.avg_pow_discharge = CMAvg(24*3600*1000) #average calculation of dischrged [kWh]
		self.avg_pow_surplus = CMAvg(24*3600*1000) #average calculation of purplus power [kWh]

		self.charge = {}
		self.charge['chargeA'] = 0  # [A] discharge[-] charge[+]
		self.charge['chargeP'] = 0  # [W] discharge[-] charge[+]
		self.charge['chargeSetA'] = 0 # [A] configured and readed value [A]
		self.charge['chargedKWh'] = 0 # charged [kWh] per 24h
		self.charge['dischargedKWh'] = 0 # discharged [kWh] per 24h
		self.charge['surplusP'] = 0 # surplus [VA] pos: battery can't consume all the grid power
		self.charge['surplusKWh'] = 0 # surplus summing [kWh] per 24h (only positve values will be appended)
		self.charge_pow_set = 0 # last setter of charge value from mqtt
		#self.charge_pow_surplus = 0 # [W] surplus calculation
		self.charge_saturation = 0 # [W] level of chagre saturation, gap between set power and charge power, always positve
		self.pow_last_grid_value = 0 # [W] last grid power value, only for statistics

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
		self.cfg_tmo_charge_ms = 2000 #timeslice update charge values



	# read from bic some common stuff
	# 	@topic-pub <main-app>/inv/<id>/info
	def	update_info(self):
		if self.bic is not None:
			dinf=self.bic.dump()
			if dinf is not None:
				self.info.update(dinf)
				if self.cc is not None:
					self.info['ChargeCtrlName'] = str(self.cc.obj_name)
			#lg.info(str(self.info))

			jpl = json.dumps(self.info, sort_keys=False, indent=4)
			global mqttc
			mqttc.publish(MQTT_T_APP + '/inv/' + str(self.id) +  '/info',jpl,0,True) # retained
		return

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
			try:
				volt = round(float(self.bic.vread()) / 100,2)
				amp = round(float(self.bic.cread()) / 100,2)
				ac_grid = round(float(self.bic.acvread()) / 10,0)

				self.state['acGridV'] = ac_grid	# grid-volatge [V]
				self.state['dcBatV'] = volt 	# bat voltage DV [V]
				self.state['capBatPc'] = self.bat.get_capacity_pc(volt)  	# bat capacity [%] , attach CBattery object
			except Exception as err:
				lg.error("dev can't read value:" + str(err))
				return

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
			try:
				volt = round(float(self.bic.vread()) / 100,2)
				amp = round(float(self.bic.cread()) / 100,2)
				self.state['dcBatV'] = round(volt,1) 	# bat voltage DV [V]
				self.charge['chargeA'] = round(amp,1)  	# bat [A] discharge[-] charge[+] ?
				pow_w = round(amp * volt)
				self.charge['chargeP'] = pow_w  # bat [VA] discharge[-] charge[+]
				_pow_surplus = 0

				cdir = self.bic.BIC_chargemode_read()
				if cdir == CBic.e_charge_mode_charge:
					amp = round((self.bic.charge_current(CBic.e_cmd_read) / 100),2)
					self.avg_pow_charge.push_val(pow_w)
					self.avg_pow_discharge.push_val(0)
					self.charge_saturation = self.charge_pow_set - pow_w
					if (self.charge_saturation >=80) and (self.pow_last_grid_value <0):
						_pow_surplus = abs(self.pow_last_grid_value)
						self.avg_pow_surplus.push_val(_pow_surplus)
					else:
						self.avg_pow_surplus.push_val(0)
					# W/ms -> kW/h
					self.charge['chargedKWh'] = round(self.avg_pow_charge.sum_get(0,0)/(1E6*3600),1)
					self.charge['surplusKWh'] = round(self.avg_pow_surplus.sum_get(0,0)/(1E6*3600),1)
				else:
					self.charge_saturation = 0
					self.avg_pow_discharge.push_val(pow_w)
					self.avg_pow_charge.push_val(0)
					self.avg_pow_surplus.push_val(0)
					self.charge['dischargedKWh'] = round(self.avg_pow_discharge.sum_get(0,0) / (1E6*3600),1)
					amp = round((self.bic.discharge_current(CBic.e_cmd_read) / 100) * (-1),2)

				self.charge['surplusP'] = _pow_surplus
				self.charge['chargeSetA'] = amp # [A] configured and readed value [A]
			except Exception as err:
				lg.error("dev update can't read value:" + str(err))
				return
		else:
			#self.state['dcBatV'] = 0
			self.charge['chargeA'] = 0
			self.charge['chargeP'] = 0
			self.charge['chargeSetA'] = 0
			self.charge['surplusP'] = 0

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
	def cfg(self,ini,reload = False):
		lg.info('cfg id:' + str(self.id))
		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		self.cfg_max_vcharge100 = ini.get_int('DEVICE',kpfx("ChargeVoltage"),self.cfg_max_vcharge100)
		self.cfg_min_vdischarge100 = ini.get_int('DEVICE',kpfx("DischargeVoltage") ,self.cfg_min_vdischarge100)

		self.cfg_max_ccharge100 = ini.get_int('DEVICE',kpfx('MaxChargeCurrent'),self.cfg_max_ccharge100)
		self.cfg_max_cdischarge100 = ini.get_int('DEVICE',kpfx('MaxDischargeCurrent'),self.cfg_max_cdischarge100)
		self.top_inv = MQTT_T_APP + '/inv/' + str(self.id)

		self.bat.cfg(ini,reload)

		lg.info("init " + str(self))
		#dischargedelay = int(config.get('Settings', 'DischargeDelay'))

	def __str__(self):
		return "dev id:{} cfg-cv:{} cfg-dv:{} cc:{} cfg-dc:{}".format(self.id,self.cfg_max_vcharge100,self.cfg_min_vdischarge100,self.cfg_max_ccharge100,self.cfg_max_cdischarge100)

	def start(self):
		lg.info('dev id:{} start'.format(self.id))
		CBic.can_up(self.can_chan_id,250000)
		self.bic = CBic(self.can_chan_id,self.can_adr)
		if self.bic is None:
			raise RuntimeError('dev init can at startup')
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

	def stop(self):
		lg.warning("device stoped id:" + str(self.id))
		self.charge_set_idle()
		#self.bic.operation(0)
		self.onl_mode = CBicDevBase.e_onl_mode_offline
		self.update_state()

	# poll values from bic/inverter
	# @topic-pub <main-app>/inv/<id>/fault
	def poll(self,timeslive_ms):

		if  self.bic is None:
			lg.error('dev bic is not started')
			os._exit(1)

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
		if self.onl_mode >= CBicDevBase.e_onl_mode_idle:
			try:
				val_amp = round(val_amp,1)
				if ((val_amp * 10) % 2) >0:
					val_amp+=0.1  # allow only even last digit (reduce eeprom writes for doubble values)
					val_amp=round(val_amp,1)
				amp100 = int(val_amp * 100)
				lg.info("dev set charge value to:{}[A]".format(val_amp))
				if amp100 >=0:
					if amp100 > self.cfg_max_ccharge100:
						amp100 = self.cfg_max_ccharge100
						lg.warning("dev max charge reached set charge value to:{}A".format(val_amp))
					elif amp100 < self.cfg_min_ccharge100:
						amp100=self.cfg_min_ccharge100
					self.bic.BIC_chargemode(CBic.e_charge_mode_charge)
					self.bic.charge_current(CBic.e_cmd_write,amp100)
				elif amp100 < 0:
					amp100 = abs(amp100)
					if amp100 > self.cfg_max_cdischarge100:
						amp100 = self.cfg_max_cdischarge100
						lg.warning("dev max discharge reached set discharge value to:{}A".format(-amp100 / 100))
					elif amp100 < self.cfg_min_cdischarge100:
						amp100=self.cfg_min_cdischarge100
					self.bic.BIC_chargemode(CBic.e_charge_mode_discharge)
					self.bic.discharge_current(CBic.e_cmd_write,amp100)
				return 0
			except Exception as err:
				lg.error("dev can't set charge value:" + str(err))


		lg.error("dev can't set charge value:" + str(val_amp))

		return -1


	# set charging to neutral position nearby 0.8A charging
	def charge_set_idle(self):
		try:
			self.bic.charge_current(CBic.e_cmd_write,self.cfg_min_ccharge100)
			self.bic.discharge_current(CBic.e_cmd_write,self.cfg_min_cdischarge100)
			self.bic.BIC_chargemode(CBic.e_charge_mode_charge)
		except Exception as err:
			lg.error("dev can't set idle value:" + str(err))

	def charge_set_pow(self,val_pow:int):
		if self.charge_pow_set == val_pow:
			return
		self.charge_pow_set = val_pow
		v = self.state['dcBatV']
		if v < 20:
			v = 24 # set before read from bic
		amp = int(val_pow) / round(v,1)  # used normaly the real running voltage
		#print('calcP:' + str(val_pow) + ' amp:' + str(amp))
		self.charge_set_amp(amp)

	def grid_pow_set_value(self,pow_val :int):
		self.pow_last_grid_value = pow_val


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
	def cfg(self,ini,reload = False):
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
	def cfg(self,ini,reload = False):
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

""" @todo Charge Controller Profile
 - Set some charge parameter for each hour of day
 - MaxDischa
"""
class CCCProfile():
	lst_hour = [] # 24 profiles for each hour
	hour_now = -1 # actual hour
	def __init__(self,hour, cc):
		self.id =0 # future use
		self.hour = hour # [0..23] hour of day
		self.pow_charge_max = 0  # [W]
		self.pow_discharge_max = 0 # [W] (negative power value)
		self.pow_grid_offset = 0 # [W] # grid power offset

	""" configure all hours
		Y=* defaulting for all hours else use the hour of day [0..23]
		- [CHARGE_CONTROL]/Id/X/Profile/Hour/Y/MaxChargePower  def:0 [W]
		- [CHARGE_CONTROL]/Id/X/Profile/Hour/Y/MaxDischargePower  def:0 [W]
		- [CHARGE_CONTROL]/Id/X/Profile/Hour/Y/GridOffsetPower  def:0 [W] grid power offset, will be added to the power value of the smart meter

	"""
	@staticmethod
	def cfg(ini):
		def kpfx(hour : int,str_tail : str):
			id = 0
			return "Id/0/Profile/Hour/{}/{}".format(hour,str_tail)

		CCCProfile.lst_hour.clear()
		CCCProfile.hour_now = -1
		pow_charge_max_def = 0 # ini.get_int('CHARGE_CONTROL',kpfx('*','MaxChargePower'),0)
		pow_discharge_max_def = 0 # = ini.get_int('CHARGE_CONTROL',kpfx('*','MaxDischargePower'),0)
		pow_grid_offset_def = 0

		for h in range(0,24):
			cprof = CCCProfile(h,None)
			#print(str(kpfx(str(h),'MaxChargePower')))
			cprof.pow_charge_max = ini.get_int('CHARGE_CONTROL',kpfx(str(h),'MaxChargePower'),pow_charge_max_def)
			cprof.pow_discharge_max = ini.get_int('CHARGE_CONTROL',kpfx(str(h),'MaxDischargePower'),pow_discharge_max_def)
			cprof.pow_grid_offset = ini.get_int('CHARGE_CONTROL',kpfx(str(h),'GridOffsetPower'),pow_grid_offset_def)
			pow_charge_max_def = cprof.pow_charge_max
			pow_discharge_max_def = cprof.pow_discharge_max
			pow_grid_offset_def = cprof.pow_grid_offset
			CCCProfile.lst_hour.append(cprof)

		CCCProfile.dump()


	def __str__(self):
		sret = '{}[h] pCharge:{}[W] pDischarge:{}[W] pOffset:{}[W]'.format(self.hour,self.pow_charge_max,self.pow_discharge_max,self.pow_grid_offset)
		return sret

	# @return true if the hour has changed
	@staticmethod
	def hour_changed():
		now =  datetime.now()
		if now.hour != CCCProfile.hour_now:
			CCCProfile.hour_now = now.hour
			return True
		return False

	@staticmethod
	def hour_get():
		CCCProfile.hour_changed() # set actual hour
		return CCCProfile.lst_hour[CCCProfile.hour_now]

	@staticmethod
	def dump():
		lg.info('Charge-Profile:')
		for cprof in CCCProfile.lst_hour:
			lg.info(str(cprof))


"""
BIC control and regulation class
- control the bic charge and discharge function
"""
class CChargeCtrlBase():

	DEF_GRID_TMO_SEC = 320 # seconds to switch off bic-dev if we get no new gid-power values
	DEF_BLOCK_TIME_DISCHARGE = 60 # [s]

	def __init__(self,dev_bic : CBicDevBase):
		self.enabled = False # enabled
		self.dev_bic = dev_bic # device2 control
		self.id = dev_bic.id
		self.obj_name = "ChargeBase" # to display the name in mqtt
		self.tmo_grid_sec = CChargeCtrlBase.DEF_GRID_TMO_SEC # grid tmo timer
		self.grid_pow = 0 # last grid power value
		self.grid_pow_tmo = False # no new values from smart-meter
		self.calc_pow = 0 # calculated power to set

		self.ts_1000ms=0 # timeslice 1000ms [ms]
		self.ts_calc_sec=0 # timeslice for each calculation [s]
		self.ts_calc_cfg=12 # timeslice cfg
		self.ts_1min=0 # timeslive 1min [s]

		self.avg_pow = CMAvg(3600*1000) #average calculation , store values for on hour
		self.pow_grid_offset = 0 # [W] offset power for the calculation, move the zero point of power balance
		self.charge_pow_tol = 10 # [W] don't set new charge value if the running one is nearby
		# will be set later with the charge profile
		self.charge_pow_max = 0 # [W] max charge power power value
		self.discharge_pow_max = 0 # [W] min discharge power value, normaly a negative value

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
	@param dbkey-int [CHARGE_CONTROL]Id/X/Type def:"PID" possible controller [None,Winter,PID]
	not used yet @param dbkey-int [CHARGE_CONTROL]Id/X/TimeSliceCalcSec def:12[s] timeslice for each calculation [s]

	@param dbkey-int [CHARGE_CONTROL]Id/X/DischargeBlockTimeSec def: 60[s] skip short discharge bursts
	@param dbkey-int [CHARGE_CONTROL]Id/X/ChargePowerOffset def: 0[W] offset power for the calculation, move the zero point of power balance
	@param dbkey-int [CHARGE_CONTROL]Id/X/ChargeTol def: 10[W] don't set new charge value if the running one is nearby

	@param dbkey-int [CHARGE_CONTROL]Id/X/DischargeBlockHourStart def:-1 [0..23] start interval hour of day to block discharging
	@param dbkey-int [CHARGE_CONTROL]Id/X/DischargeBlockHourStop def:-1  [0..23] stop interval hour of day to block discharging

	"""
	def cfg(self,ini,reload = False):
		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		if reload is False:
			top_pow = ini.get_str('CHARGE_CONTROL',kpfx('TopicPower'),MQTT_APP_ID)
			msg = CMQTT.CMSG(top_pow,"dummypl")
			msg.cb = self.cb_mqtt_sub_power
			msg.cb_user_data = self.on_cb_grid_power
			global mqttc
			mqttc.append_subscribe(msg)
			lg.info('CC grid power topic:' + str(top_pow))
		else:
			self.reset()

		self.enabled = True
		self.ts_calc_cfg = ini.get_int('CHARGE_CONTROL',kpfx('TimeSliceCalcSec'),self.ts_calc_cfg)

		self.discharge_block_tmo_cfg = ini.get_int('CHARGE_CONTROL',kpfx('DischargeBlockTimeSec'),CChargeCtrlBase.DEF_BLOCK_TIME_DISCHARGE)
		self.pow_grid_offset = 0 # will be set via profile ini.get_int('CHARGE_CONTROL',kpfx('ChargePowerOffset'),self.pow_grid_offset)
		self.charge_pow_tol = ini.get_int('CHARGE_CONTROL',kpfx('ChargeTol'),self.charge_pow_tol)
		self.lst_discharge_block_hour[0] = ini.get_int('CHARGE_CONTROL',kpfx('DischargeBlockHourStart'),-1) # invalidate as default
		self.lst_discharge_block_hour[1] = ini.get_int('CHARGE_CONTROL',kpfx('DischargeBlockHourStop'),-1) # invalidate as default
		CCCProfile.cfg(ini)
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

	def reset(self):
		lg.info('CC charge control reset')
		self.dev_bic.charge_set_idle() # reset to lowest charge value
		self.calc_pow = 0

	def enable(self,enable :bool):
		self.enabled=enable
		lg.info('CC charge control enable:' +str(enable))
		self.reset()

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

			#print(str(self.tmo_grid_sec))
			if self.tmo_grid_sec >= 0:
				self.tmo_grid_sec-=1
				if self.tmo_grid_sec < 0:
					self.on_cb_grid_power_tmo()
		return


	# new power value from grid:
	# payload: try to parse a simple value in [W]
	def cb_mqtt_sub_power(self,mqttc,user_data,mqtt_msg):
		try:
			self.grid_pow = int(mqtt_msg.payload)
			self.dev_bic.grid_pow_set_value(self.grid_pow)
			self.tmo_grid_sec = CChargeCtrlBase.DEF_GRID_TMO_SEC
			self.grid_pow_tmo = False
			self.on_cb_grid_power(self.grid_pow)
		except ValueError:
			pass

	""" received a new value from the grid power sensor
		power value: >0 receive power from the public-grid
		power value: <0 inject power to the public-grid
	"""
	def on_cb_grid_power(self,grid_pow):
		self.tmo_grid_sec = CChargeCtrlBase.DEF_GRID_TMO_SEC
		#lg.info('CC new grid power value {}[W]'.format(self.grid_pow))

	# grid power smart meter timeout reset charge/discharge level until new values arrived
	def on_cb_grid_power_tmo(self):
		lg.warning('CC grid power TMO')
		self.grid_pow_tmo = True
		self.reset()
		#os._exit(2)
		return



"""
BIC control and regulation class
- usefull for preservation the battery voltage in winter time
- define min/max capacity, start charging <=min and charge until max capacity
- check temperature of the bic and charge only if it is highter than...
- cfg: charge-power value, min/max capacity, min temperature
"""
class CChargeCtrlWinter(CChargeCtrlBase):
	eSM_ChageCtrlInit		= 0 # init, stop charging
	eSM_ChageCtrlCheckDelay	= 1 # app start, check capacity of the bat first
	eSM_ChageCtrlDischarge 	= 2 # discharge, if capacity is higher than maxCapacity
	eSM_ChageCtrlCharge		= 3 # capacity is lower than maxCapacity
	eSM_ChageCtrlStoped		= 4 # capacity is bweteen minCapacity and maxCapacity

	MIN_TEMP_C = 10 # minimum temperature [C]

	sCharge = ['Init','Check','Discharge','Charge','Stoped'] # enum to string

	def __init__(self,dev_bic : CBicDevBase):
		super().__init__(dev_bic)
		self.obj_name = "ChargeWinter" # to display the name in mqtt
		self.sm =  CChargeCtrlWinter.eSM_ChageCtrlCheckInit
		self.sm_tmo_delay_sec = 6 # [s] timer to sleep,delay the state machine
		self.sm_tmo_messure_delay_sec = 120 # voltage messure delay for idle-bat volatage
		self.cfg_min_temp_c = CChargeCtrlWinter.MIN_TEMP_C # minimum temperature [C] for charging
		self.cfg_min_cap_pc = 30 # min. bat capacity [%], < goto state charge
		self.cfg_max_cap_pc = 50 # max. allow  bat capacity [%], > stop charging max-cap + 20%: discharge
		self.cfg_const_pow = 200 # charge discharge power [W]

	""" Charge Control Winter: cfg
	    @param dbkey-int [CHARGE_CONTROL]Id/X/Winter/ChargeP def:200W [VA]
		@param dbkey-int [CHARGE_CONTROL]Id/X/Winter/TempMin def:10 [C]
		@param dbkey-int [CHARGE_CONTROL]Id/X/Winter/CapMin def:20 [%]
		@param dbkey-int [CHARGE_CONTROL]Id/X/Winter/CapMax def:50 [%]
	"""
	def cfg(self,ini,reload = False):

		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		super().cfg(ini)
		self.cfg_min_temp_c = ini.get_int('CHARGE_CONTROL',kpfx('Winter/TempMin'),CChargeCtrlWinter.MIN_TEMP_C)
		self.cfg_const_pow = ini.get_int('CHARGE_CONTROL',kpfx('Winter/ChargeP'),200)
		self.cfg_min_cap_pc = ini.get_int('CHARGE_CONTROL',kpfx('Winter/CapMin'),20)
		self.cfg_max_cap_pc = ini.get_int('CHARGE_CONTROL',kpfx('Winter/CapMax'),50)

	""" received a new value from the grid power sensor
		don't used it for the winter
	"""
	def on_cb_grid_power(self,grid_pow):
		# nothing todo
		pass
		#super().on_cb_grid_power(grid_pow)

	def check_delay_waiting(self):
		if self.sm_tmo_delay_sec >=0:
			self.sm_tmo_delay_sec-=1
			return True
		else:
			self.sm_tmo_delay_sec=-1
			return False

	# check temperature value for charging
	def check_temp_ok(self):
		temp_c = -256
		try:
			temp_c = int(self.dev_bic.state['tempC'])
			if temp_c > self.cfg_min_temp_c:
				return True
		except:
			pass
		lg.critical('temperature to low, waiting t:{}[C]'.format(temp_c))
		self.sm_tmo_delay_sec = 60 * 10
		return False




	""" simple charge discharge control:
		-triiger this function from app-poll ?
		- check min/max capacity
		- @todo: implement state machine
	"""
	def calc_power(self,grid_pow):

		is_running = super().calc_power(grid_pow)
		if is_running is False:
			return

		charge_pow = self.dev_bic.charge['chargeP']
		cap_bat_pc = int(self.dev_bic.state['capBatPc'])
		new_calc_pow = 0

		if App.ts_1min == 2:
			lg.info('bat cap:{}[%] pCharge:{} state:{}'.format(cap_bat_pc,charge_pow,CChargeCtrlWinter.sCharge[self.sm]))

		if self.sm == CChargeCtrlWinter.eSM_ChageCtrlInit:
			# wait a bit and set the charge-level to 0
			if self.check_delay_waiting() is True:
				return
			self.dev_bic.charge_set_pow(0)
			self.sm_tmo_delay_sec=2*60
			self.sm = CChargeCtrlWinter.eSM_ChageCtrlCheckDelay
		elif self.sm == CChargeCtrlWinter.eSM_ChageCtrlCheckDelay: # app start, check capacity of the bat first
			if self.check_delay_waiting() is True:
				return
			# start delay reached check temp and voltage
			if self.check_temp_ok() is False:
				return
			# temp ok, check bat capactity

			new_pow = 0
			if cap_bat_pc > self.cfg_max_cap_pc+20: # don't allow to mutch bat capacity in winter
				self.sm = CChargeCtrlWinter.eSM_ChageCtrlDischarge # discharge, if capacity is higher than maxCapacity
				new_calc_pow = abs(self.cfg_const_pow) * (-1)
			elif cap_bat_pc <= self.cfg_min_cap_pc:
				self.sm = CChargeCtrlWinter.eSM_ChageCtrlCharge # charge, if capacity is lower than minCapacity
				new_calc_pow = abs(self.cfg_const_pow) * (+1)
			else:
				self.sm = CChargeCtrlWinter.eSM_ChageCtrlStoped # bat-cap level is ok
				new_calc_pow = 0
				self.sm_tmo_delay_sec=3600

			lg.info('bat cap:{}[%] min/max:{}/{} new state:{}'.format(cap_bat_pc,self.cfg_min_cap_pc,self.cfg_max_cap_pc,CChargeCtrlWinter.sCharge[self.sm]))
			self.dev_bic.charge_set_pow(new_calc_pow)
		elif self.sm == CChargeCtrlWinter.eSM_ChageCtrlCharge: # capacity is lower than maxCapacity
			if (cap_bat_pc -10) >= self.cfg_max_cap_pc:
				self.sm = CChargeCtrlWinter.eSM_ChageCtrlCheckDelay
				lg.info('Stop charging reached:{}[%]'.format(cap_bat_pc))
				self.dev_bic.charge_set_pow(0)
				self.sm_tmo_delay_sec=3600
		elif self.sm == CChargeCtrlWinter.eSM_ChageCtrlDischarge: # capacity is lower than maxCapacity
			if (cap_bat_pc-10) <= self.cfg_max_cap_pc:
				self.sm = CChargeCtrlWinter.eSM_ChageCtrlCheckDelay
				lg.info('Stop discharging reached:{}[%]'.format(cap_bat_pc))
				self.dev_bic.charge_set_pow(0)
				self.sm_tmo_delay_sec=3600
		elif self.sm == CChargeCtrlWinter.eSM_ChageCtrlStoped:	# capacity is between minCapacity and maxCapacity
			if self.check_delay_waiting() is False:
				self.sm = CChargeCtrlWinter.eSM_ChageCtrlCheckDelay
			#print('stoped:' + str(self.sm_tmo_delay_sec) + ' S:' + str(self.sm))
		else:
			raise RuntimeError('wrong sm-state:' + str(self.sm))


		return

	# assume 1sec. slice
	def poll(self,timeslice_ms):
		super().poll(timeslice_ms)
		self.calc_power(0)



"""
BIC control and regulation class
- simple one charge and discharge depends on power grid value
"""
class CChargeCtrlSimple(CChargeCtrlBase):

	def __init__(self,dev_bic : CBicDevBase):
		super().__init__(dev_bic)
		self.cfg_loop_gain = 0.5 # regulator loop gain for new values
		self.obj_name = "ChargeSimple"

	""" Charge Control Simple:
	"""
	def cfg(self,ini,reload = False):

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
	def on_cb_grid_power(self,grid_pow):
		def avg2min(minute : int):
			return self.avg_pow.avg_get(minute*60*1000,-1)

		super().on_cb_grid_power(grid_pow)

		#lg.info('CC new grid power value {} [W]'.format(self.grid_pow))
		self.avg_pow.push_val(grid_pow)

		lg.info('CC pGrid:{}[W] pGridAvg:1m:{} 2m:{} 5m:{} 1h:{} offs:{}[W]'.format(grid_pow,avg2min(1),avg2min(2),avg2min(5),avg2min(60),self.pow_grid_offset))
		if self.enabled is True:
			self.calc_power(grid_pow)



	""" simple charge discharge control:
		- new charge value = grid-power * (-1)
		- discharge block time, skip fast charge, discharge toggle
	"""
	def calc_power(self,grid_pow):

		is_running = super().calc_power(grid_pow)
		if is_running is False:
			return

		charge_pow = self.dev_bic.charge['chargeP']
		grid_pow = self.avg_pow.avg_get(1*1000*60,-1) + self.pow_grid_offset # used avg grid power with offset


		new_calc_pow = math_round_up(charge_pow  + (grid_pow * self.cfg_loop_gain * (-1)))

		""" testcode simple linear control
		#pdiff = round(grid_pow - self.calc_pow,-1)
		sign=CChargeCtrlBase.sign(grid_pow)
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

		POW_LIMIT = 800
		new_cal_pow=CChargeCtrlBase.clamp(new_cal_pow,-POW_LIMIT,POW_LIMIT)

		# check and skip short discharge burst e.g. use the grid power for the tee-kettle

		if new_calc_pow < 0:
			if self.discharge_blocking_state() is True:
				new_calc_pow = 0

		print('d:{} tol:{}'.format((charge_pow - new_calc_pow),self.charge_pow_tol))
		if abs(int(grid_pow - self.pow_grid_offset)) > self.charge_pow_tol:
			lg.info('CC set new value: pGrid:{}[W] pBat:{}[W] pCalc:{}[W] pOfs:{}[W]'.format(grid_pow,charge_pow,new_calc_pow,self.pow_grid_offset))
			topic = self.dev_bic.top_inv + '/charge/set'
			dpl = {"var":"chargeP"}
			dpl['val'] = int(new_calc_pow)
			#print("top:{} pl:{}".format(topic,str(dpl)))
			global mqttc
			mqttc.publish(topic,json.dumps(dpl, sort_keys=False, indent=0),0,False) # no retain
		else:
			lg.info('CC const value: pGrid:{}[W] pBat:{}[W] pCal:{}[W] oOfs:{}[W]'.format(grid_pow,charge_pow,new_calc_pow,self.pow_grid_offset))
		self.calc_power_set(new_calc_pow)
		return


	def poll(self,timeslice_ms):
		super().poll(timeslice_ms)


""" simple pid regulator
	Howto setup pid regulator
	  - kiss (KeepItSimpeAndStupid) use only K(e.g. 0.6) , set I,D to zero
	  - https://belektronig.de/wp-content/uploads/2023/03/Manuelle-Bestimmung-von-PID-Parametern.pdf
      - https://www.jumo.de/web/services/faq/controller/pid-controller

"""
class CPID :

	def __init__(self):
		self.cfg_dt  = 0 	# force time-between steps else it will be messured
		self.cfg_offset = 0 # const-offset
		self.cfg_min = 0		# min allow value
		self.cfg_max = 0		# max allow value
		self.cfg_kp  = 1		# K-Part gain
		self.cfg_ki  = 0		# I-Part gain set 0 for simple K-regulator
		self.cfg_kd  = 0		# D-Part gain set 0 for simple K-regulator

		self.err = 0.0			# last error values step(t-1)
		self.I_val = 0.0		# I-part Value
		self.t_step = datetime.now()
		self.cnt_steps=0

	def reset(self):
		self.err = 0.0
		self.I_val = 0.0
		self.t_step = datetime.now() # step calculation for cfg_dt
		self.cnt_step = 0

	# configuration and reset of the pid
	def cfg(self, dt_sec,offset, vmin, vmax, kp, ki, kd):
		def rnd(val):
			return round(val,1)

		self.reset()
		self.cfg_dt  = int(dt_sec) 	# >0 force this time-between steps
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

		def rnd(val):
			return round(val,1)

		def clamp(val, minn, maxn):
			new_val = max(min(maxn, val), minn)
			if new_val != val:
				#lg.warning("pid reached min/max val:{} new:{}".format(val,new_val))
				pass
			return new_val

		def get_dt():
			if self.cfg_dt >0:
				_dt = self.cfg_dt
			else:
				_dt = CChargeCtrlBase.get_time_diff_sec(self.t_step)
			return _dt

		if self.cnt_step ==0: # skip first loop
			self.t_step = datetime.now()
			self.cnt_step+=1
			return 0

		_dt = get_dt()

		_err = self.cfg_offset - act_val
		P = self.cfg_kp * _err

		"""
		# faster I Value reset
		if  (CChargeCtrlBase.sign(_err) != CChargeCtrlBase.sign(self.I_val)) and (self.cfg_ki >0):
			self.I_val=0
			lg.debug("pid rst I")
		"""

		# calculate I-Part
		self.I_val += self.cfg_ki * _err * _dt
		self.I_val = clamp(self.I_val,self.cfg_min / 4, self.cfg_max / 4)
		I = self.I_val

		if _dt >0:
			D = self.cfg_kd * (_err - self.err) / _dt
		else:
			D = 0

		ret_val = P + I + D
		ret_val=clamp(ret_val,self.cfg_min,self.cfg_max)

		self.err = _err
		self.cnt_step += 1
		self.t_step = datetime.now() # dynamic step calculation for _dt
		lg.debug("pid stp v:{} dt:{}[s] stp:{} p:{} i:{} d:{} err:{} ret:{}[W]".format(
				act_val,_dt,self.cnt_step,rnd(P),rnd(I),rnd(D),rnd(_err),rnd(ret_val)
		))
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
		self.grid_pow_last = 0 # grid power value t-1
		self.obj_name = "ChargePID"


	""" Charge Control Simple:
		# not used @param dbkey-int [CHARGE_CONTROL]Id/X/Pid/ClockSec def:0
		@param dbkey-int [CHARGE_CONTROL]Id/X/Pid/MaxChargePower def:400[W] (relative for each step)
		@param dbkey-int [CHARGE_CONTROL]Id/X/Pid/MaxDischargePower def:-400[W] (relative for each step)
		@param dbkey-float [CHARGE_CONTROL]Id/X/Pid/P def:1
		@param dbkey-float [CHARGE_CONTROL]Id/X/Pid/I def:0
		@param dbkey-float [CHARGE_CONTROL]Id/X/Pid/D def:0
		"""
	def cfg(self,ini,reload = False):

		def kpfx(str_tail : str):
			return "Id/{}/{}".format(self.id,str_tail)

		super().cfg(ini)
		self.pid.cfg(
			ini.get_int('CHARGE_CONTROL',kpfx('Pid/ClockSec'),0), # 0, means messure time between each step
			self.pow_grid_offset,
			ini.get_int('CHARGE_CONTROL',kpfx('Pid/MaxDischargePower'),-400),
			ini.get_int('CHARGE_CONTROL',kpfx('Pid/MaxChargePower'),400),
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
	def on_cb_grid_power(self,grid_pow):

		def avg2min(minute : int):
			return self.avg_pow.avg_get(minute*60*1000,-1)

		super().on_cb_grid_power(grid_pow)

		#lg.info('CC new grid power value {} [W]'.format(self.grid_pow))
		#grid_pow=self.sm_zero_tol(grid_pow)
		self.avg_pow.push_val(grid_pow)
		lg.info('CC pGrid:{}[W] pGridAvg:1m:{} 2m:{} 5m:{} 1h:{} offs:{}[W]'.format(grid_pow,avg2min(1),avg2min(2),avg2min(5),avg2min(60),self.pow_grid_offset))
		#grid_pow=self.sm_zero_tol(grid_pow)
		if self.enabled is True:
			self.calc_power(grid_pow)
		self.grid_pow_last = grid_pow


	""" @audit-ok but not used try to fix the offset problem of my smart-meter at point zero
		- this point is very unstable (the sign calculation)
		- try to fix the pid-regulator between the offset value:
		-30 -20 0 0 0 +20 30
		-       ^ ^ ^   offset:+-20 set all values between offset to zero
		@return modifyed grid power value
	"""
	def sm_zero_tol(self,grid_pow):
		sm_offset=10
		if (grid_pow >= (sm_offset * (-1))) and (grid_pow <= sm_offset):
			return 0
		else:
			return grid_pow

	""" reset the pid regulator if the sign of the grid is
		changing and the power value is high
		@return True if the direction has changed at an high power-value
	"""
	def grid_power_dir_changed(self,grid_pow_now : int):
		if abs(grid_pow_now) >= 100 and self.grid_pow_last !=0:
			if CChargeCtrlBase.sign(grid_pow_now) != CChargeCtrlBase.sign(self.grid_pow_last):
				return True
		return False


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
		tol_pow=int(grid_pow - self.pow_grid_offset)

		if self.grid_power_dir_changed(grid_pow) is True:
			lg.critical("CC grid power changed direction:pid reset")
			self.reset()
			new_calc_pow = 0
			charge_pow = 0
		elif abs(tol_pow) > self.charge_pow_tol:
			new_calc_pow = self.calc_pow + self.pid.step(grid_pow)
			#new_calc_pow = charge_pow + self.pid.step(grid_pow)
		else:
			lg.debug('CC pid stoped tol:{}[W]'.format(tol_pow))
			self.pid.reset()
			return

		# prevent, that the set values running away from the real bat-chargeing level, the charge power is limited
		new_calc_pow=CChargeCtrlBase.clamp(round(new_calc_pow,-1),round(charge_pow-100-1),round(charge_pow+100,-1))
		#use also the configured min/max one top
		new_calc_pow=CChargeCtrlBase.clamp(new_calc_pow,self.discharge_pow_max, self.charge_pow_max)

		# check and skip short discharge burst e.g. use the grid power for the tee-kettle
		if new_calc_pow < 0:
			if self.discharge_blocking_state() is True:
				new_calc_pow = 0
				self.pid.reset()
			elif self.discharge_pow_max >=0:
				lg.debug('CC no-discharge hour profile')

		if abs(tol_pow) > self.charge_pow_tol:
			lg.info('CC set new value: pGrid:{}[W] pBat:{}[W] pCalcNew:{}[W] pOfs:{}[W]'.format(grid_pow,charge_pow,new_calc_pow,self.pow_grid_offset))
			topic = self.dev_bic.top_inv + '/charge/set'
			dpl = {"var":"chargeP"}
			dpl['val'] = int(new_calc_pow)
			#print("top:{} pl:{}".format(topic,str(dpl)))
			global mqttc
			mqttc.publish(topic,json.dumps(dpl, sort_keys=False, indent=0),0,False) # no retain
			self.calc_power_set(new_calc_pow)
		else:
			lg.info('CC const value: pGrid:{}[W] pBat:{}[W] pCalcLast:{}[W] pOfs:{}[W]'.format(grid_pow,charge_pow,new_calc_pow,self.pow_grid_offset))
		self.calc_power_set(new_calc_pow)

		return

	def poll(self,timeslice_ms):
		super().poll(timeslice_ms)
		if self.ts_1min == 1:
			if CCCProfile.hour_changed() is True:
				cprof = CCCProfile.hour_get()
				lg.info('new hour profile:' + str(cprof))
				self.discharge_pow_max = cprof.pow_discharge_max
				self.charge_pow_max = cprof.pow_charge_max
				self.pow_grid_offset = cprof.pow_grid_offset
				self.pid.cfg_offset = cprof.pow_grid_offset

	def reset(self):
		super().reset()
		self.pid.reset()

	def enable(self,enable):
		super().enable(enable)
		self.pid.reset()


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

	def __init__(self,cmqtt,ini):
		self.cmqtt = cmqtt
		#self.ini = ini
		#self.id= ini.get_str('MQTT','AppId',MQTT_APP_ID)
		self.t_start =  datetime.now()   # time.localtime()
		self.info = {}
		self.started = False
		self.dev_bic = {} # all bic hardware devices
		self.ini = ini

	def stop(self):
		for dev in self.dev_bic.values():
			dev.stop()

	""" BIC Config
		[DEVICE]
		@param dbkey-str [DEVICE]Id/X/Type def:empty well known modem type "BIC2200"
		@param dbkey-int [DEVICE]Id/X/CanBaudRate def:0 Baudrate
		@topic-sub <main-app>/inv/<id>/charge/set {"var":[chargeA,chargeP],"val":[ampere or power]]}
	"""
	def cfg(self,ini,reload = False):

		if self.ini is None:
			self.ini = ini
		if reload is True:
			self.ini.reload()
			for dev in self.dev_bic.values():
				dev.cfg(self.ini,True)
				dev.cc.cfg(self.ini,True)
			return

		# @future-use iterate over all bic's
		id = 0
		dev_type = ini.get_str('DEVICE','Id/{}/Type'.format(id),"")
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

		cc_type = ini.get_str('CHARGE_CONTROL','Id/{}/Type'.format(id),"PID").lower()
		if cc_type == 'pid':
			dev.cc = CChargeCtrlPID(dev)
		elif cc_type == 'winter':
			dev.cc = CChargeCtrlWinter(dev)
			#dev.cc = CChargeCtrlSimple(dev)
		else:
			dev.cc = None
		if dev.cc is not None:
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
					elif dpl['var'] == 'cfgReload':
						self.cfg(self.ini,True) # config reload
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

# roundup 56->60
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
	fname_ini = "./" + APP_NAME + '.ini'
	if len(sys.argv) >=2:
		fname_ini = sys.argv[1]
		print("{} set ini file name:{}".format(sys.argv[0],fname_ini))
	ini = CIni(fname_ini)

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
	app = App(mqttc,ini)
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


