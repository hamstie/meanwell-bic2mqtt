#!/usr/bin/env python3
VER_CMQTT = '1.3'

# import mqtt
import paho.mqtt.client as mqtt
import json

"""
 - MQTT Client Object module
   hamstie fst:fst:04.04.2024 lst:08.04.2024
   + add user_data for subscribe
"""
class CMQTT:

	class CMSG:
		def __init__(self,topic,payload):
			self.topic=topic
			self.topic_tok = [] # list for e.g. subscribed topic, pre-parsed topic foo/bar -> ['foo','bar'], don't need it for publish
			self.payload=payload
			self.qos=0 # default QoS:0
			self.retain=False
			self.cb=None # for subscribed messages callback function to call
			self.cb_user_data = None
			
		def pp(self):
			return str("top:'" + self.topic + "' pl:'" + self.payload + "'")

		def tokenize(self):
			self.topic_tok=self.topic.split('/')


	def __init__(self,id,user_data=None):
		self.id=id.lower()
		#self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, id,user_data) # for module version >=2.X
		self.mqtt = mqtt.Client(id,user_data) # for module version <2.X
		self.d_offline_queue = {} # store msg's in offlien mode and publish it if the connection to broker is online again 

		self.lwt_msg_online = None # last will topic
		self.lwt_msg_offline = None # last will topic

		self.dTopic = {} # dictionary for all received topics
		self.dSubscEvent = {} # key:topic, val: callback to inform app that this toppic was subscribed
		self.is_connected = False
		self.conn_cnt =0

		self.user_data = user_data

		self.mqtt.on_connect=self.__on_connect__
		self.mqtt.on_disconnect=self.__on_disconnect__
		self.mqtt.on_message=self.__on_message__

	# internal
	def __on_connect__(self,client, userdata, flags, rc):
		self.is_connected = True
		self.conn_cnt += 1
		
		_topics = []
		for msg in self.dSubscEvent.values():
			_topics.append((msg.topic,0)) # append tupel (topic,qos)
		self.mqtt.subscribe(_topics)
		
		#client.subscribe('foo/sysio/' + "#")

		if self.lwt_msg_online is not None:
			self.publish_msg(self.lwt_msg_online)

		for msg in self.d_offline_queue.values():
			self.publish_msg(msg)
			del(msg.topic) 

		if self.on_connect is not None:
			self.on_connect(self,self.user_data)

	# internal
	def __on_disconnect__(self,client, userdata, rc):
		self.is_connected=False
		if self.on_disconnect is not None:
			self.on_disconnect(self,self.user_data,rc)

	# internal
	def __on_message__(self,client, userdata, msg):
		
		# topic compare for foo/+/bar, foo/# and foo/bar topics
		def match_topic(lst_tok_topic,new_msg):
			match = False
			lst_new = new_msg.topic_tok
			pos=0
			for it in lst_tok_topic:
				if it=='+':
					pos+=1
					continue
				elif it=='#':
					break
				elif pos >=len(lst_new):
					match=False
					break
				elif it==lst_new[pos]:
					match=True
					pos+=1
				else:
					match=False
					break
			"""	
			if match is False:
				print(str(lst_tok_topic))
				print(new_topic)
			"""
			return match

		
		top=str(msg.topic)
		#print('t:' + top)
		pl=str(msg.payload.decode('UTF-8'))
		subsc_msg = self.dSubscEvent.get(top,None) # obj is a hal-device 
		new_msg=CMQTT.CMSG(top,pl)
		new_msg.retain=msg.retain
		new_msg.tokenize()
		if subsc_msg is not None:
			## since V1.1 subsc_msg.cb.cb_mqtt_sub_event(self,self.user_data,new_msg)  # direct string match between topic and key
			subsc_msg.cb(self,subsc_msg.cb_user_data,new_msg)  # direct string match between topic and key
		else: # possible to subscribe a bunch of topics via 'foo/#' or 'foo/+/bar'
			for k,subsc_msg in self.dSubscEvent.items():
				if match_topic(subsc_msg.topic_tok,new_msg) is True:
					subsc_msg.cb(self,subsc_msg.cb_user_data,new_msg)
					# inform all break # single call ?
		
		if self.on_message is not None:
			self.on_message(self,self.user_data,top,pl)


	# if the appened toppic was subscribed/received from broker, this callback will be triggered
	# obj_func=obj.cb_mqtt_sub_event(mqttc,user_data,mqtt_msg)
	def append_subscribe_topic(self,top : str,obj_func):
		msg = CMQTT.CMSG(top,'pldummy')
		msg.tokenize()
		msg.cb=obj_func
		self.dSubscEvent[top]=msg
		self.mqtt.subscribe(top)
	
	# if the appened toppic was subscribed/received from broker, this callback will be triggered
	# obj_func=obj.cb_mqtt_sub_event(mqttc,user_data,mqtt_msg)
	def append_subscribe(self,msg : CMSG):
		if msg.cb is None or msg.topic is None or len(msg.topic)==0:
			raise RuntimeError("invalid mqtt-msg") 
		
		msg.tokenize()
		self.dSubscEvent[msg.topic]=msg
		self.mqtt.subscribe(msg.topic)


	def set_auth(self,user,passwd):
		self.mqtt.username_pw_set(user,passwd)

	#  set last will and testament if pl_online is defined publish this after connect/reconnect
	def set_lwt(self,topic,pl_offline,pl_online=None):
		if len(topic) and len(pl_offline):
			self.lwt_msg_offline=CMQTT.CMSG(topic,pl_offline)
			self.lwt_msg_offline.retain=True			 
			if	pl_online is not None:	
				self.lwt_msg_online=CMQTT.CMSG(topic,pl_online)
				self.lwt_msg_online.retain=True

			return 0
		else:
			return -1

	def connect(self,ip_adr,port=1883,tmo=60):
		if self.lwt_msg_offline is not None:
			self.mqtt.will_set(self.lwt_msg_offline.topic, payload=self.lwt_msg_offline.payload, qos=0, retain=True)
		self.mqtt.connect(str(ip_adr), port, tmo)
		self.mqtt.loop_start() # Start loop and receive the retained messages

	def stop(self):
		self.mqtt.disconnect()
		# ~ self.mqtt.loop_stop()

	# app overwrite this 
	def on_connect(self,mqtt,user_data):
		pass

	# app overwrite this
	def on_disconnect(self,mqtt,user_data,rc):
		pass
	# app overwrite this
	def on_message(self,mqtt,user_data,pl,topic):
		pass

	def publish(self,topic,pl,qos=0,retain=False):	
		return self.mqtt.publish(topic,pl,qos,retain)

	# big advanatge store msg in offline mode is possible, after changed to online, republish the values
	def publish_msg(self,msg):
		if self.is_connected is False:
			self.d_offline_queue[msg.topic] = msg
			return None
		
		return self.mqtt.publish(msg.topic,msg.payload,0,msg.retain)


	@staticmethod	
	def payload_json_get_key(pl,key,def_val):
		try:
			jpl = json.loads(pl)
			return jpl.get(key,def_val)
		except:
			return def_val

		return 	

	""" try to parse simple set and state keys from json """
	@staticmethod	
	def payload_parser_json(pl,def_val):
		high={'1','on','running','ok'}
		low={'0','off','online','offline','err'} # pt online to low because running is the normal working mode
		
		try:
			jpl = json.loads(pl)
		except:
			return def_val
			
		try:
			ret_set = str(jpl.get('set','')).lower()
			if len(ret_set):
				if ret_set in high:
					return 1
				elif ret_set in low:
					return 0	
		except:
			return def_val
		
		try:
			ret_state = str(jpl.get('state','')).lower()
			if ret_state in high:
				return 1
			elif ret_state in low:
				return 0

		except:
			return def_val

		return def_val


	""" try to parse payload raw  and json
		- non parsed payload will return the default value
		- 0,off,OFF,offline,OFFLINE,err,ERR return 0
		- 1,on,ON,running,ok,OK return 1
		- {*} this is json payload, parse:
		- set:[0,1]
		- state:"online",offline 
	"""
	@staticmethod
	def payload_parser(pl,def_val):
		high={'1','on','running','ok'}
		low={'0','off','online','offline','err'} # pt online to low because running is the normal working mode
		
		if pl is None or len(pl) ==0:
			return def_val
		
		if pl[0] == '{' and pl[-1] == '}':
			return CMQTT.payload_parser_json(pl,def_val)

		pl_low=pl.lower()
		if pl_low in high:
			return 1
		elif pl_low in low:	
			return 0

		return




		""" try to parse simple set and state keys from json 
			-jkey {"jkey":[0,1,on,off]}
		"""
	@staticmethod	
	def payload_parser_json2(pl,jkey,def_val):
		high={'1','on','running','ok'}
		low={'0','off','online','offline','err'} # pt online to low because running is the normal working mode
		
		try:
			jpl = json.loads(pl)
		except:
			return def_val
			
		try:
			ret_set = str(jpl.get(jkey,'')).lower()
			if len(ret_set):
				if ret_set in high:
					return 1
				elif ret_set in low:
					return 0	
		except:
			pass

		return def_val


	""" try to parse payload raw and json for io's
		- non parsed payload will return the default value
		- 0,off,OFF @return 0
		- 1,on,ON, @return 1
		- {*} this is json payload, parse:
		- jkey:[0,1,on,off]
		@return <0 for errors
	"""
	@staticmethod
	def payload_parser_io(pl,jkey='val'):
		high={'1','on','running','ok'}
		low={'0','off','online','offline','err'} # pt online to low because running is the normal working mode
		
		if pl is None or len(pl) ==0:
			return -1
		
		if pl[0] == '{' and pl[-1] == '}':
			return CMQTT.payload_parser_json2(pl,jkey,-2)

		pl_low=pl.lower()
		if pl_low in high:
			return 1
		elif pl_low in low:	
			return 0
