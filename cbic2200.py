#!/usr/bin/env python3
# cbic2200.py
# Controlling the Mean Well BIC-2200-CAN
# tested only with the 24V Version BIC-2200-CAN-24
# Please note:  this software to control the BIC-2200 is not yet complete
# and also not fully tested. The BIC-2200 should not be operated unattended.
# There is no error handling yet !!!

# What is missing:
# - error handling
# - variables plausibility check
# - programming missing functions
# - current and voltage maximum settings
VER = "0.2.791"
# steve 08.06.2023  Version 0.2.1
# steve 10.06.2023  Version 0.2.2
# macGH 15.06.2023  Version 0.2.3
#       - support for Meanwell NPB-x Charger
#       - new config area
# steve 16.06.2023  Version 0.2.4
#       - fault and status queries
# steve 19.06.2023  Version 0.2.4
#       - fault queries completed
# steve 09.07.2023  Version 0.2.5
#       - init_mode added
# steve 20.07.2023 Version 0.2.6
#       - directionread
#       - statusread
#       - can_receive_byte
# steve 06.02.2024 Version 0.2.7
#       - rename first variable statusread to outputread
#       - statusread now fully functional
# hamstie 16.04.2024 Version 0.2.72 class implementation of bic2200.py
#       - worked as a module
#       + add exception for can read timeouts
#       - removed some boilercode
#       - return list of fault-bits for sttus processing
#       + dump()
#       - init_mode try to disable parameter eeprom write mode
# hamstie 29.04.2024 Version 0.2.74
#       + can_send_receive word(), skip useless eeprom writes and check the value with write read sequence
#       - toggle-operation
# hamstie 4.5.2024 catch index error if can read failed
# hamstie 26.05.2024 Version 0.2.76 skip useless eeprom writes for the direction mode
# hamstie 10.07.2024 Version 0.2.77 catch value error
# hamstie 30.07.2024 Version 0.2.78 rs232-can device shutdown, wrong function was called
# hamstie 28-09-2024 Version 0.2.791 fan read function (only working later "firmRev": "0xd0e")

import os
import can
import sys
import time

error = 0

#########################
# MAIN CONFIG
#########################

#########################
#ID = Cortroller Message ID + Device ID [00-07]
#Be sure you select the right CAN_ADR and add your Device-ID (Jumper block)
#BIC-2200 00 - 07, NPB 00 - 03
#
#BIC-2200
CAN_ADR = 0x000C0300
#
#NPB-1200
#CAN_ADR = 0x000C0103
#########################

#########################
#If you use a RS232 to CAN Adapter which ich socketCAN compartible, switch to 1
#e.g. USB-Tin www.fischl.de
# If you use a CAN Hat (waveshare) set USE_RS232_CAN = 0
#Add the rigth /dev/tty device here
USE_RS232_CAN = 0
CAN_DEVICE = '/dev/ttyACM0'

#########################

def bic22_commands():
    print("")
    print(" " + sys.argv[0] + " - controlling the BIC-2200-CAN Bidirectional Power Supply")
    print("")
    print(" Usage:")
    print("        " + sys.argv[0] + " parameter and <value>")
    print("")
    print("       on                   -- output on")
    print("       off                  -- output off")
    print("       outputread           -- read output status 1:on 0:off")
    print("")
    print("       cvread               -- read charge voltage setting")
    print("       cvset <value>        -- set charge voltage")
    print("       ccread               -- read charge current setting")
    print("       ccset <value>        -- set charge current")
    print("")
    print("       dvread               -- read discharge voltage setting")
    print("       dvset <value>        -- set discharge voltage")
    print("       dcread               -- read discharge current setting")
    print("       dcset <value>        -- set discharge current")
    print("")
    print("       vread                -- read DC voltage")
    print("       cread                -- read DC current")
    print("       acvread              -- read AC voltage")
    print("")
    print("       charge               -- set direction charge battery")
    print("       discharge            -- set direction discharge battery")
    print("       dirread              -- read direction 0:charge,1:discharge ")
    print("")
    print("       tempread             -- read power supply temperature")
    print("       fanread              -- read fan 1 and 2 speed [rpm]")
    print("       typeread             -- read power supply type")
    print("       dump                 -- dump supply info type, software, revision")
    print("       statusread           -- read power supply status")
    print("       faultread            -- read power supply fault status")
    print("")
    print("       can_up               -- start can bus")
    print("       can_down             -- shut can bus down")
    print("")
    print("       init_mode            -- init BIC-2200 bi-directional battery mode")
    print("")
    print("       <value> = amps or volts * 100 --> 25,66V = 2566")
    print("")
    print("       Version {} ".format(VER))

#########################################
# gereral function
def set_bit(value, bit):
    return value | (1<<bit)

def clear_bit(value, bit):
    return value & ~(1<<bit)

def get_normalized_bit(value, bit_index):
    return (value >> bit_index) & 1

def get_high_low_byte(val: int):
    hb = int(val) >> 8
    lb = int(val) & 0xff
    return (hb,lb)

#########################################
# bic class
class CBic:
    e_charge_mode_charge=0
    e_charge_mode_discharge=1

    e_cmd_read = 0
    e_cmd_write = 1

    e_state_off = 0
    e_state_on  = 1

    # used CAN commands:
    e_cmd_OPERATION =           0x0000 # operation on/off
    e_cmd_VOUT_SET =            0x0020 # @notice: eeprom write vout set
    e_cmd_IOUT_SET =            0x0030 # @notice: eeprom write eeprom write
    e_cmd_FAULT_STATUS =        0x0040 # fault status register
    e_cmd_READ_VIN =            0x0050 # AC voltage reading value
    e_cmd_READ_VOUT =           0x0060 # DC voltage reading value
    e_cmd_READ_IOUT =           0x0061 # DC current reading value
    e_cmd_READ_TEMPERATURE_1 =  0x0062 # read temperature
    e_cmd_READ_FAN1 =           0x0070 # fan speed fan1
    e_cmd_READ_FAN2 =           0x0071 # fan speed fan2
    e_cmd_DIRECTION_CTRL =      0x0100 # charge discharge direcrion control
    e_cmd_REVERSE_VOUT_SET =    0x0120 # @notice: eeprom write
    e_cmd_REVERSE_IOUT_SET =    0x0130 # @notice: eeprom write
    # ....

    def __init__(self,can_chan_id='can0' ,can_adr=CAN_ADR):
        self.can_chan = None
        self.can_chan_id = can_chan_id
        self.can_adr = can_adr
        self.persist = True # for command line switch  to true (another error handling for can read/write errors)
        self.fault_changed = True # fault was changed update fault if this flag was set
        self.d_fault = {} # key is the name value is a tupel of active(1) and fault-count
        self.d_fault['fan'] =    {'active':0,'cnt':0,'desc':"fanspeed abnormal"}
        self.d_fault['otp'] =    {'active':0,'cnt':0,'desc':"over temperature protection"}
        self.d_fault['otpHi'] =  {'active':0,'cnt':0,'desc':"internal hi temperature protection"}
        self.d_fault['ovp'] =    {'active':0,'cnt':0,'desc':"over voltage protection"}
        self.d_fault['ovpHi'] =  {'active':0,'cnt':0,'desc':"over voltage protection"}
        self.d_fault['olp'] =    {'active':0,'cnt':0,'desc':"over current protection"}
        self.d_fault['short'] =  {'active':0,'cnt':0,'desc':"short circuit protection"}
        self.d_fault['acRange'] ={'active':0,'cnt':0,'desc':"ac grid range"}
        self.d_fault['dcOff'] =  {'active':0,'cnt':0,'desc':"dc off"}
        self.d_fault['eeprom'] = {'active':0,'cnt':0,'desc':"eeprom fault"}
        self.d_fault['can'] =    {'active':-1,'cnt':0,'desc':"can-com error / read-tmo"} # -1 change on startup

        self.write_cnt = 0 # write counter for persistent mode
        self.d_info = {} # modelName,firmRev....

        try:
            self.can_chan = can.interface.Bus(channel = self.can_chan_id, bustype = 'socketcan')
        except Exception as e:
            print(e)
            print("CAN INTERFACE NOT FOUND. TRY TO BRING UP CAN DEVICE FIRST WITH -> can_up")
            sys.exit(2)

    # init can device
    @staticmethod
    def can_up(can_chan_id = 'can0',bit_rate = 250000):
        print('can up:{} bit-rate:{}'.format(can_chan_id,bit_rate))
        os.system('sudo ip link set {} up type can bitrate {}'.format(can_chan_id,bit_rate))
        os.system('sudo ifconfig {} txqueuelen 65536'.format(can_chan_id))

    # init serial can device
    @staticmethod
    def can_up_serial(can_chan_id = 'can0',dev_node = CAN_DEVICE):
        os.system('sudo slcand -f -s5 -c -o ' + dev_node)
        os.system('sudo ip link set up {}'.format(can_chan_id))

    @staticmethod
    def can_down(can_chan_id = 'can0'):
        os.system('sudo ip link set {} down'.format(can_chan_id))

    def can_shutdown_serial(self):
        if self.can_chan is not None:
            self.can_chan.shutdown()

    def can_send_msg(self,lst_data):
        msg = can.Message(arbitration_id=self.can_adr, data=lst_data, is_extended_id=True)

        try:
            self.can_chan.send(msg)
        except can.CanError:
            print("CAN send error")
            raise RuntimeError("can't send can message")


    """ receive 16Bit word from can
        return default value on any read error
    """
    def can_receive_word(self,e_cmd :int,default = None):
        cmd_hb,cmd_lb = get_high_low_byte(e_cmd)
        self.can_send_msg([cmd_lb,cmd_hb])
        v=self.can_receive()
        if v is None:
            return default
        return v

    """
    read a word if it is equal to val, do nothing (force is False)
    - send the value and read the returned value
    - raise exception if given and received value are not equal
    - return True for success
    """
    def can_send_receive_word(self,cmd :int,val:int,force=False):
    
        def eeprom_write_check(cmd):
            if cmd == CBic.e_cmd_VOUT_SET or cmd == CBic.e_cmd_IOUT_SET: 
                self.write_cnt+=1
            elif cmd == CBic.e_cmd_REVERSE_VOUT_SET or cmd == CBic.e_cmd_REVERSE_IOUT_SET:
                self.write_cnt+=1

        cmd_hb,cmd_lb = get_high_low_byte(cmd)
        val_hb,val_lb = get_high_low_byte(val)

        # check running value
        if force is False:
            self.can_send_msg([cmd_lb,cmd_hb])
            vr=self.can_receive()
            if vr == val:
                #print("skip vr:{} val:{}".format(vr,val))   
                return True

        # set new value
        self.can_send_msg([cmd_lb,cmd_hb,val_lb,val_hb])
        eeprom_write_check(cmd)

        # check if value was set
        self.can_send_msg([cmd_lb,cmd_hb])
        vr=self.can_receive()
        #print("vr:{} val:{}".format(vr,val))        
        if vr != val:
            raise RuntimeError("can't set value command:{} vr:{} val:{}".format(hex(cmd),vr,val))
        
        return True

    #@return list of values
    def can_rcv_raw(self, tmo=0.5):
        msgr = self.can_chan.recv(tmo)
        if msgr is None:
            print('Timeout occurred, no message.')
            if self.persist is False:
                sys.exit(2)
            raise TimeoutError()
        return str(msgr).split()

    
    # receive function @reurn int value
    def can_receive(self):
        try:
            msgr_split = self.can_rcv_raw()
        except TimeoutError:
            return None
        if len(msgr_split) < 12:
            return None
        #print(msgr_split)
        try:
        #print (str(hexval))
            hexval = int((msgr_split[11] + msgr_split[10]),16)
        except (AttributeError, ValueError):
            return None
        return hexval

    # receive function
    def can_receive_byte(self):
        try:
            msgr_split = self.can_rcv_raw()
        except TimeoutError:
            return None
        if len(msgr_split) < 11:
            return None
        hexval = (msgr_split[10])
        #print (int(hexval,16))
        return int(hexval,16)


    # receive function
    def can_receive_char(self):
        try:
            msgr_split = self.can_rcv_raw()
            s = bytearray.fromhex(msgr_split[10]+msgr_split[11]+msgr_split[12]+msgr_split[13]+msgr_split[14]+msgr_split[15]).decode()
        except (TimeoutError,IndexError):
            return None

        #print(s)
        return s

    # Operation function
    # @return read value from bic
    def operation(self,val):#0=off, 1=on, 2=toggle
        # print ("turn output on/off")
        # Command Code 0x0000  e_cmd_OPERATION

        if val == 2:
            val = self.operation_read()
            val = not val 
        else:        
            val = val & 0x01
        
        commandhighbyte = 0x00
        commandlowbyte = 0x00
        self.can_send_msg([commandlowbyte, commandhighbyte,val])
        val = self.operation_read()
        return val

    def operation_read(self):
        # print (Read status "output on/off")
        # Command Code 0x0000
        commandhighbyte = 0x00
        commandlowbyte = 0x00

        self.can_send_msg([commandlowbyte, commandhighbyte])
        v = self.can_receive_byte()
        return v

    # charge voltage, max. volatge level of battery
    def charge_voltage(self,rw,val=0):
        # print ("read/set charge voltage")
        # Command Code 0x0020 e_cmd_VOUT_SET
        # Read Charge Voltage
        commandhighbyte = 0x00
        commandlowbyte = 0x20

        if rw==CBic.e_cmd_read:
            self.can_send_msg([commandlowbyte, commandhighbyte])
            return self.can_receive()
        else:
            val=int(val)
            self.can_send_receive_word(CBic.e_cmd_VOUT_SET,val)
            return val

    def charge_current(self,rw,val=0): #0=read, 1=set
        # print ("read/set charge current")
        # Command Code 0x0030 IOUT_SET EEPROM write !!!
        # Read Charge Voltage
        commandhighbyte = 0x00
        commandlowbyte = 0x30

        if rw==CBic.e_cmd_read:
            self.can_send_msg([commandlowbyte,commandhighbyte])
            return self.can_receive()
        else:
            val=int(val)
            self.can_send_receive_word(CBic.e_cmd_IOUT_SET,val)
            return val

   
            
    # set the minimum volatage of the bat in discharge mode
    def discharge_voltage(self,rw,val=0): #0=read, 1=set
        # print ("read/set discharge voltage")
        # Command Code 0x0120 REVERSE_VOUT_SET EPPROM write !!!
        # Read Charge Voltage
        commandhighbyte = 0x01
        commandlowbyte = 0x20

        if rw==CBic.e_cmd_read:
            self.can_send_msg([commandlowbyte,commandhighbyte])
            return self.can_receive()
        else:
            val=int(val)
            self.can_send_receive_word(CBic.e_cmd_REVERSE_VOUT_SET,val)
            return val

    def discharge_current(self,rw,val=0): #0=read, 1=set
        # print ("read/set charge current")
        # Command Code 0x0130 REVERSE_IOUT_SET EEPROM set !!
        # Read Charge Voltage
        commandhighbyte = 0x01
        commandlowbyte = 0x30

        if rw==CBic.e_cmd_read:
            self.can_send_msg([commandlowbyte,commandhighbyte])
            return self.can_receive()
        else:
            val = int(val)
            self.can_send_receive_word(CBic.e_cmd_REVERSE_IOUT_SET,val)
            """
            valhighbyte = int(val) >> 8
            vallowbyte  = int(val) & 0xFF
            v=val
            self.can_send_msg([commandlowbyte,commandhighbyte,vallowbyte,valhighbyte])
            """
            return int(val)


    def vread(self):
        # print ("read dc voltage")
        # Command Code 0x0060
        # Read DC Voltage

        commandhighbyte = 0x00
        commandlowbyte = 0x60
        self.can_send_msg([commandlowbyte,commandhighbyte])
        return self.can_receive()

    def cread(self):
        # print ("read dc current")
        # Command Code 0x0061
        # Read DC Current

        commandhighbyte = 0x00
        commandlowbyte = 0x61

        self.can_send_msg([commandlowbyte,commandhighbyte])

        msgr_split = self.can_rcv_raw()
        if msgr_split is None:
            return None

        hexval = (msgr_split[11]+ msgr_split[10])

        # quick and primitive solution to determine the
        # negative charging current when discharging the battery

        cval = (int(hexval,16))
        if cval > 20000 :
            cval = cval - 65536

        #print (cval)
        return cval


    def acvread(self):
        # print ("read ac voltage")
        # Command Code 0x0050
        # Read AC Voltage

        commandhighbyte = 0x00
        commandlowbyte = 0x50

        self.can_send_msg([commandlowbyte,commandhighbyte])
        return self.can_receive()


    # sys config: check(and set) eeprom write flag
    # battery-mode: check and set birirect-mode
    def init_mode(self):

        self.can_send_msg([0xC2,0x00]) #  SYS-Config
        sys_cfg = self.can_receive()
        
        if sys_cfg is None:
            print("ERROR ini_mode")
            return None

        print('syscfg:' + hex(sys_cfg))
        sys_cfg_h = int(sys_cfg) >> 8
        sys_cfg_l  = int(sys_cfg) & 0xFF

        flag_can_ctrl = get_normalized_bit(int(sys_cfg_l), bit_index=0)
        if flag_can_ctrl == 0:
            sys_cfg_l = set_bit(sys_cfg_l,0)
            print('ini_mode can control disabled -> enabled') 
            self.can_send_msg([0xC2,0x00,sys_cfg_l,sys_cfg_h])
            time.sleep(1)

        # (only working later "firmRev": "0xd0e")
        flag_eeprom_write = get_normalized_bit(int(sys_cfg_h), bit_index=2)
        if flag_eeprom_write ==0:
            # write value to eeprom enabled
            print('ini_mode write parameter to eeprom enabled -> disabled')
            #sys_cfg_h = sys_cfg_h  & ~(1 << 2) # clear bit 10
            sys_cfg_h = set_bit(sys_cfg_h,2)
            self.can_send_msg([0xC2,0x00,sys_cfg_l,sys_cfg_h])
            time.sleep(1)


        self.can_send_msg([0x040,0x01]) # bidirectional battery mode config
        cfg_bm = self.can_receive()

        if cfg_bm is None:
            print("ERROR can't init mode")
            return None

        flag_bidirect = get_normalized_bit(int(cfg_bm), bit_index=0)
        if flag_bidirect ==0:
            print('ini_mode enable bidirect mode, need repowering !!!')
            #cfg_bm = cfg_bm | 0x01 # set bit 0
            cfg_bm = set_bit(cfg_bm,0)
            cfg_bm_h = int(cfg_bm) >> 8
            cfg_bm_l  = int(cfg_bm) & 0xFF
            self.can_send_msg([0x40,0x01,cfg_bm_l,cfg_bm_h])
            time.sleep(1)
            #exit(0)


        if self.persist is False:
            print("init_mode done")
        return 0


    def BIC_chargemode(self,val): #0=charge, 1=discharge
        # print ("set charge/discharge")
        # Command Code 0x0100
        # Set Direction Charge

        val = int(val)

        cmd_hb,cmd_lb = get_high_low_byte(CBic.e_cmd_DIRECTION_CTRL)    
        # check running value
        self.can_send_msg([cmd_lb,cmd_hb])
        vr=self.can_receive_byte()
        if vr == val:
            #print("skip vr:{} val:{}".format(vr,val))   
            return val

        # set new value
        self.can_send_msg([cmd_lb,cmd_hb,val])
        self.write_cnt+=1
        return val

    def BIC_chargemode_read(self):
        # print ("read charge/discharge mode")
        # Command Code 0x0100
        # Read Direction charge/discharge
        cmd_hb,cmd_lb = get_high_low_byte(CBic.e_cmd_DIRECTION_CTRL)    

        self.can_send_msg([cmd_lb, cmd_hb])
        v = self.can_receive_byte()
        return v


    def NPB_chargemode(self,rw, val=0xFF):
        # print ("Set PSU or Charger Mode to NPB Device")
        # Command Code 0x00B4
        commandhighbyte = 0x00
        commandlowbyte = 0xB4

        #first Read the current value
        self.can_send_msg([commandlowbyte, commandhighbyte])
        v = int(self.can_receive(),16)

        if rw==CBic.e_cmd_write: #0=read, 1=write
            #modify Bit 7 of Lowbyte
            if val==0xFF: val=int(sys.argv[3])
            if val==1:
                v = set_bit(v,7)
            else:
                v = clear_bit(v,7)

            valhighbyte = v >> 8
            vallowbyte = v & 0xFF

            #send to device
            self.can_send_msg([commandlowbyte,commandhighbyte,vallowbyte,valhighbyte])
            self.e_cmd_write += 1
            #check the current value
            self.can_send_msg([commandlowbyte,commandhighbyte])
            v = int(self.can_receive(),16)

        return v

    def dump(self):

        commandhighbyte = 0x00
        commandlowbyte = 0x82

        self.can_send_msg([commandlowbyte,commandhighbyte])
        s1 = self.can_receive_char()

        commandlowbyte = 0x83
        self.can_send_msg([commandlowbyte,commandhighbyte])
        s2 = self.can_receive_char()

        if s1 is None or s2 is None:
            return None

        s=s1+s2
        self.d_info['modelName'] = s

        # firmware version

        try:
            self.can_send_msg([0x84,0x00])
            self.d_info['firmRev'] = hex(self.can_receive()) # to bytes hexvalue mcu0 and mcu1

            self.can_send_msg([0xC2,0x00])
            self.d_info['sysCfg'] = hex(self.can_receive()) # to bytes hexvalue mcu0 and mcu1

            self.can_send_msg([0x40,0x01])
            self.d_info['sysCfgBDir'] = hex(self.can_receive()) # bdir config bit 1
        except (AttributeError, ValueError):
            return None

        self.can_send_msg([0x86,0x00])
        self.d_info['manDate'] = str(self.can_receive_char()) # manufac. date

        self.d_info['cntWrite'] = self.write_cnt

        if self.persist is False:
            print('dev-info:' + str(self.d_info))

        return self.d_info

    def typeread(self):
        # print ("read power supply type")
        # Command Code 0x0082
        # Command Code 0x0083
        # Read Type of PSU

        commandhighbyte = 0x00
        commandlowbyte = 0x82

        self.can_send_msg([commandlowbyte,commandhighbyte])
        s1 = self.can_receive_char()

        commandlowbyte = 0x83
        self.can_send_msg([commandlowbyte,commandhighbyte])
        s2 = self.can_receive_char()

        if s1 is None or s2 is None:
            return None
        s=s1+s2
        #print(s)
        return s

    def tempread(self):
        return self.can_receive_word(CBic.e_cmd_READ_TEMPERATURE_1,-278 * 10)

    # @eturn fan1,fan2 speed
    def fanread(self,silence = False):
        # print ("read fan speed")
        v1 = self.can_receive_word(CBic.e_cmd_READ_FAN1,0)
        v2 = self.can_receive_word(CBic.e_cmd_READ_FAN2,0)

        if silence is False:
            print('fan1:{} fan2:{}'.format(v1,v2))
        return v1,v2


    # @return list of the affected bits
    def statusread(self,silence = False):
        # print ("Read System Status")
        # Command Code 0x00C1
        # Read System Status

        commandhighbyte = 0x00
        commandlowbyte = 0xC1

        self.can_send_msg([commandlowbyte,commandhighbyte])
        sval = self.can_receive()

        if sval is None:
            return None

        if silence is True:
            return self.fault_changed

        # deconding
        s = get_normalized_bit(int(sval), bit_index=0)
        if s == 0:
            print ("Current Device is Slave")
        else:
            print ("Current Device is Master")

        s = get_normalized_bit(int(sval), bit_index=1)
        if s == 0:
            print ("Secondary DD output Status TOO LOW")
        else:
            print ("Secondary DD output Status NORMAL")

        s = get_normalized_bit(int(sval), bit_index=2)
        if s == 0:
            print ("Primary PFC OFF oder abnormal")
        else:
            print ("Primary PFC ON normally")

        s = get_normalized_bit(int(sval), bit_index=3)
        if s == 0:
            print ("Active Dummy Load off / not_supported")
        else:
            print ("Active Dummy Load on")

        s = get_normalized_bit(int(sval), bit_index=4)
        if s == 0:
            print ("Device in initialization status")
        else:
            print ("NOT in initialization status")

        s = get_normalized_bit(int(sval), bit_index=5)
        self.fault_update('eeprom',s)
        if s == 0:
            print ("EEPROM data access normal")
        else:
            print ("EEPROM data access error")

        return self.fault_changed

    """ update fault dir
        @return True if fault-entry has changed
    """
    def fault_update(self,name,new_state):
        fault = self.d_fault[name]
        if fault['active'] != new_state:
            if new_state >0:
                fault['cnt'] +=1
            fault['active'] = new_state
            self.fault_changed = True
            print('fault state changed {} = {} cnt:{}'.format(name,new_state,fault['cnt']))
            return self.fault_changed
        return False

    """ Read System Fault Status
        Command Code 0x0040
        - set and count faults
        @return true if something has changed
    """
    def faultread(self):
        self.fault_changed = False
        commandhighbyte = 0x00
        commandlowbyte = 0x40

        self.can_send_msg([commandlowbyte,commandhighbyte])
        sval = self.can_receive()
        if sval is None:
            return self.fault_update('can',1)
        else:
            self.fault_update('can',0)

        # decoding
        s = get_normalized_bit(int(sval), bit_index=0)
        self.fault_update('fan',s)

        s = get_normalized_bit(int(sval), bit_index=1)
        self.fault_update('otp',s)

        s = get_normalized_bit(int(sval), bit_index=2)
        self.fault_update('ovp',s)

        s = get_normalized_bit(int(sval), bit_index=3)
        self.fault_update('olp',s)

        s = get_normalized_bit(int(sval), bit_index=4)
        self.fault_update('short',s)

        s = get_normalized_bit(int(sval), bit_index=5)
        self.fault_update('acRange',s)

        s = get_normalized_bit(int(sval), bit_index=6)
        self.fault_update('dcOff',s)

        s = get_normalized_bit(int(sval), bit_index=7)
        self.fault_update('otpHi',s)
        s = get_normalized_bit(int(sval), bit_index=8) 
        self.fault_update('ovpHi',s)
        if self.persist is False:
            for fault in self.d_fault.values():
                print(str(fault))

        return self.fault_changed


def command_line_argument(bic):

    def pp(str_out : str):
        print(str(str_out))

    if len (sys.argv) == 1:
        print ("")
        print ("Error: First command line argument missing.")
        bic22_commands()
        error = 1
        return

    bic.persist = False

    if   sys.argv[1] in ['on']:        bic.operation(1)
    elif sys.argv[1] in ['off']:       bic.operation(0)
    elif sys.argv[1] in ['outputread']:pp(bic.operation_read())
    elif sys.argv[1] in ['cvread']:    pp(bic.charge_voltage(CBic.e_cmd_read,None))
    elif sys.argv[1] in ['cvset']:     bic.charge_voltage(CBic.e_cmd_write,sys.argv[2])
    elif sys.argv[1] in ['ccread']:    pp(bic.charge_current(CBic.e_cmd_read))
    elif sys.argv[1] in ['ccset']:     bic.charge_current(CBic.e_cmd_write,sys.argv[2])
    elif sys.argv[1] in ['dvread']:    pp(bic.discharge_voltage(CBic.e_cmd_read))
    elif sys.argv[1] in ['dvset']:     bic.discharge_voltage(CBic.e_cmd_write,sys.argv[2])
    elif sys.argv[1] in ['dcread']:    pp(bic.discharge_current(CBic.e_cmd_read))
    elif sys.argv[1] in ['dcset']:     bic.discharge_current(CBic.e_cmd_write,sys.argv[2])
    elif sys.argv[1] in ['vread']:     pp(bic.vread())
    elif sys.argv[1] in ['cread']:     pp(bic.cread())
    elif sys.argv[1] in ['acvread']:   pp(bic.acvread())
    elif sys.argv[1] in ['charge']:    bic.BIC_chargemode(CBic.e_charge_mode_charge)
    elif sys.argv[1] in ['discharge']: bic.BIC_chargemode(CBic.e_charge_mode_discharge)
    elif sys.argv[1] in ['dirread']:   pp(bic.BIC_chargemode_read())
    elif sys.argv[1] in ['tempread']:  pp(bic.tempread())
    elif sys.argv[1] in ['fanread']:   bic.fanread()
    elif sys.argv[1] in ['typeread']:  pp(bic.typeread())
    elif sys.argv[1] in ['dump']:      bic.dump()
    elif sys.argv[1] in ['statusread']:bic.statusread()
    elif sys.argv[1] in ['faultread']: bic.faultread()
    elif sys.argv[1] in ['can_up']:    CBic.can_up()
    elif sys.argv[1] in ['can_down']:  CBic.can_down()
    elif sys.argv[1] in ['init_mode']: bic.init_mode()
    elif sys.argv[1] in ['NPB_chargemode']: bic.NPB_chargemode(int(sys.argv[2]))
    else:
        print("")
        print("Unknown first argument '" + sys.argv[1] + "'")
        bic22_commands()
        error = 1
        return

#### Main
if __name__ == "__main__":
    if USE_RS232_CAN == 1:
        if sys.argv[1] in ['can_up']:
            CBic.can_up_serial()
            sys.exit(0)

    bic = CBic()
    command_line_argument(bic)

    if USE_RS232_CAN == 1:
        #shutdown CAN Bus
        bic.can_shutdown_serial()

    sys.exit(error)
