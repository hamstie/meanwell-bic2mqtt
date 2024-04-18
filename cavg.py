#!/usr/bin/env python3
from datetime import datetime
import time # for the test unit
import math

""" calculation of the avage value
	- 1ms resolution
	- parameter:max. time interval in ms
	- parameter:list length for the average 
"""
class CMAvg():
	VER=0.2
	
	def __init__(self,max_time_ms,max_values=-1):
		self.cfg_max_time_ms=max_time_ms # max. time [ms] to store, values -1: inifinite
		self.cfg_max_values=max_values  # max. values to store -1: not used
		self.lst_val = [] # list of the stored values, index 0 is the oldes entry
		self.lst_ts = [] # list of the time values
		
	# reset list
	def reset(self):
		self.lst_ts.clear()
		self.lst_val.clear()

	def __len__(self):
		return len(self.lst_val)

	def __str__(self):
		lst_print = []
		max_len = 10
		self._garbage_collector()

		for idx in range(len(self.lst_val),0,-1):
			lst_print.append(self.lst_val[idx-1])

		if len(self) >max_len:
			lst_print.append("...")

		sret = "avg:{} min,max:{} lst({}):{}".format(round(self.avg_get(),2),str(self.min_max_get()),len(self),str(lst_print))
		return sret

	def _pop(self,idx=-1):
		self.lst_val.pop(idx)
		self.lst_ts.pop(idx)

	def _get_tdiff_ms(self,ts_now,ts):
		diff = ts_now - ts
		diff_ms = diff.total_seconds() * 1000 # + diff.microseconds
		return diff_ms

	# garbage collector, remove unwanted(too old, too mutch) values
	def _garbage_collector(self):
		
		# to old ?
		if self.cfg_max_time_ms >=0:
			ts_now = datetime.now()
			cnt_del = 0
			for ts in self.lst_ts:
				diff_ms = self._get_tdiff_ms(ts_now,ts)
				if diff_ms > self.cfg_max_time_ms:
					cnt_del+=1
			
			while cnt_del >0:
				cnt_del-=1
				self._pop(0)

		# to mutch
		if self.cfg_max_values >=0:
			while len(self.lst_val) > self.cfg_max_values:
				self._pop(0)

		return self.__len__()
	
	# push new value to the list
	def push_val(self,val):
		self.lst_val.append(val)
		self.lst_ts.append(datetime.now())
		self._garbage_collector()

	""" @return avg values, if time_ms was set, only calc avg from the given time in [ms]  
	 - one ms resolution
	 -  
	+--+        +--------------+
	|  |        |       ^      |
	|  +--------+       ts     |
	|  |        |<---ts_step-->|
ms  123456789.  ts-step	       ts-srep
	
	"""
	def avg_get(self,time_ms=0):
	
		if len(self)==0:
			return 0
		
		sum = 0
		ts_now = datetime.now()
		t_sum_ms = 0
		ts_step = ts_now
		for idx in range(len(self.lst_val),0,-1):
			
			step_ms = math.ceil(self._get_tdiff_ms(ts_step, self.lst_ts[idx-1])) # return min. 1ms
			ts_step = self.lst_ts[idx-1]
			
			if time_ms > 0 and (t_sum_ms+step_ms) > time_ms:
				break

			t_sum_ms += step_ms
			sum += self.lst_val[idx-1] * step_ms
			#cnt += 1

		return (sum / t_sum_ms)

	# @return the min and max value
	# if non value in list 0,0 will be returned
	def min_max_get(self,time_ms=0):
		_min=0
		_max=0
		
		if len(self) >0:
			_min = self.lst_val[-1]
			_max = self.lst_val[-1]

		ts_now = datetime.now()
		for idx in range(len(self.lst_val),0,-1):

			if time_ms >0:
				diff_ms = self._get_tdiff_ms(ts_now,self.lst_ts[idx-1])
				if diff_ms > time_ms:
					break
			
			if _min > self.lst_val[idx-1]:
				_min = self.lst_val[idx-1]
			
			if _max < self.lst_val[idx-1]:
				_max = self.lst_val[idx-1]
		
		return (_min,_max)

	@staticmethod
	def test_unit():
		#avg = CMAvg(2*1000) # restrict to 2sec
		#avg = CMAvg(-1,20) # restrict to 20 values
		print("tu avg start")
		avg = CMAvg(-1) # no time and max value restriction
		avg.push_val(1)
		#time.sleep(3)
		avg.push_val(2)
		avg.push_val(3)
		#avg.push_val()
		#avg.avg_get()
		print(avg)
		#print("tu avg 1,2,3:" + str(avg.avg_get(2000)))
		avg.reset()
		print(avg)
		avg.push_val(-1)
		avg.push_val(2)
		#time.sleep(2)
		avg.push_val(1)
		_min,_max = avg.min_max_get(500)
		print(avg)
		print("tu min/max:{},{}".format(_min,_max))
		avg = CMAvg(500) # no time and max value restriction
		#avg.reset()
		#avg.push_val(1)
		avg.push_val(1)
		time.sleep(1)
		avg.push_val(0)
		avg.push_val(-1)
		time.sleep(0.2)
		#avg.push_val(0)
		print("tu avg get last sec:" + str(round(avg.avg_get(0),2)))

if __name__ == "__main__":
	CMAvg.test_unit()
	exit(0)
	