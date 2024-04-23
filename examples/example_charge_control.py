#!/usr/bin/env python3

# example for using bic2200.py(original) or cbic2200.py(this fork)
# Version 0.20
import sys
import time
import schedule
import json
import subprocess
import requests
import datetime
import configparser

from func_timeout import func_timeout, FunctionTimedOut

# Safe Values for Voltages and Currents 
SafeChargeVoltage = 2750
SafeDischargeVoltage = 2520
SafeChargeCurrent = 3500
SafeDischargeCurrent = 2600

config = configparser.ConfigParser()

# First init values from Config File
config.read('./charge_control.conf')
ChargeVoltage = int(config.get('Settings', 'ChargeVoltage'))
DischargeVoltage = int(config.get('Settings', 'DischargeVoltage'))
MaxChargeCurrent = int(config.get('Settings', 'MaxChargeCurrent'))
MaxDischargeCurrent = int(config.get('Settings', 'MaxDischargeCurrent'))
dischargedelay = int(config.get('Settings', 'DischargeDelay'))
DCOutput = int(config.get('Control', 'DCOutput'))


lastchargetime = time.time()     # Startzeit zur Berechnung der Einspeiseverzögerung

# dischargedelay = 10                   # Variable zu verzögerten Einspeisung um nur
                                        # bei längeren Verbräuchen einzuspeisen ( in sec)

BICAPP = "./bic2200.py" # the original one
BICAPP = "./cbic2200.py" # the new one from this fork


# Init CAN Bus
p = subprocess.run([BICAPP , "can_up"])


# Switch on Device
#if DCOutput == 1:
#    p = subprocess.run([BICAPP , "on"])
#elif DCOutput == 0:
#    p = subprocess.run([BICAPP , "off"])
#else:
#    print ("Wrong DCOutput value in charge_control.conf. 1 = on, 0 = off")

# Write Charge / Discharge Voltages
if ChargeVoltage > SafeChargeVoltage:
    ChargeVoltage = SafeChargeVoltage

if DischargeVoltage < SafeDischargeVoltage:
    DischargeVoltage = SafeDischargeVoltage
    
p = subprocess.run([BICAPP, "cvset", str(ChargeVoltage)])
p = subprocess.run([BICAPP, "dvset", str(DischargeVoltage)])

def control_power():

    #-------------------------------------------------------------- Read Config and Check Values
    config.read('./charge_control.conf')
    ChargeVoltage = int(config.get('Settings', 'ChargeVoltage'))
    DischargeVoltage = int(config.get('Settings', 'DischargeVoltage'))
    MaxChargeCurrent = int(config.get('Settings', 'MaxChargeCurrent'))
    MaxDischargeCurrent = int(config.get('Settings', 'MaxDischargeCurrent'))
    DCOutput = int(config.get('Control', 'DCOutput'))

    global lastchargetime

    if MaxChargeCurrent > SafeChargeCurrent:
        MaxChargeCurrent = SafeChargeCurrent
        print ("Charge Current too big")

    if MaxDischargeCurrent > SafeDischargeCurrent:
        MaxDischargeCurrent = SafeDischargeCurrent
        print ("Discharge Current too big")            
    

    #-------------------------------------------------------------- Read Power Meter
    # print ("Control Charge/Discharge")

    stromzaehler = requests.get("http://-- Energy Meter IP --/cm?cmnd=status%2010")
    stromz = stromzaehler.json()
    stromz1 = (stromz['StatusSNS'])
    # zeit = (stromz1['Time'])
    stromz2 = (stromz1['Haus'])
    Power = (stromz2['Power'])
    
    zeit = datetime.datetime.now()
    print (str(zeit) + ": Power: " + str(Power) + " W")

    if Power > 20000:
        Power = 20000

    #-------------------------------------------------------------- Read BIC-2200
    volt = subprocess.run([BICAPP, "vread"], capture_output=True, text=True)
    volt_now = float(volt.stdout)
    amp = subprocess.run([BICAPP, "cread"], capture_output=True, text=True)
    amp_now = float(amp.stdout)
    print ("BIC-2200 Volt: ", volt_now/100," Ampere: ", amp_now/100)
    
    #-------------------------------------------------------------- Charge / Discharge

    DiffCurrent = Power*10000/volt_now*(-1)
    Current = DiffCurrent + amp_now
    print ("Calc_Current: ", Current/100)

    if Current > 10:

        lastchargetime = time.time()

        IntCurrent = int(Current)

        if IntCurrent >= MaxChargeCurrent:
            IntCurrent = MaxChargeCurrent
            

        p = subprocess.run([BICAPP , "charge"])
        c = subprocess.run([BICAPP , "ccset" ,  str(IntCurrent)]) 

    if Current < -10:
        dischargetime = time.time()
       
        IntCurrent = int(Current*(-1)) 

        if IntCurrent >= MaxDischargeCurrent:
            IntCurrent = MaxDischargeCurrent  
        
        if dischargetime - lastchargetime > dischargedelay:         
            
            print ("Verzögerte Einspeisung")
            OutCurrent = IntCurrent
    
        else:
            print ("Warten auf Einspeisung")
            OutCurrent = 0


        p = subprocess.run([BICAPP , "discharge"])
        c = subprocess.run([BICAPP , "dcset", str(OutCurrent)]) 

             

    # ---------------------------------------------------------------------------Logging 
    logfile = open('./battery.log','a')
    logfile.write(str(zeit) + ",PowerMeter," + str(Power) + ",W,Battery_U," + str(volt_now/100) + ",V,Battery_I," + str(amp_now/100) + ",A,I Calc," + str(Current/100)+",A \n")
    logfile.close()



# schedule.every(4).seconds.do(control_power)      # Aufruf der Regelroutine ca alle 5s

while True:
     control_power()
     time.sleep(3)

     #schedule.run_pending()

