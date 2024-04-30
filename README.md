# meanwell-bic2mqtt (under construction)
Tool to control Power Supplys from Mean Well via MQTT

was forked from https://github.com/stcan/meanwell-can-control <br>

Some new features:
 - possible to use cbic2200.py as a python module
 - can bus handling 
   - read-write-read check to decrease eeprom write cycles
   - write-read check to increase stability
   - read failues will be raised a Timout or RuntimeExeption
 - disabling eeprom write access (not tested yet, no available firmware-version)

Pre-Tested with the 24V Version BIC-2200-24-CAN<br>
Please note: this tool is not yet complete and also not fully tested. <br>
Do not use without monitoring the devices. 

What is missing:
- variables plausibility check

       Usage: ./cbic2200.py parameter value
       
       on                   -- output on
       off                  -- output off
       statusread           -- read output status 1:on 0:off 

       cvread               -- read charge voltage setting
       cvset <value>        -- set charge voltage
       ccread               -- read charge current setting
       ccset <value>        -- set charge current

       dvread               -- read discharge voltage setting
       dvset <value>        -- set discharge voltage
       dcread               -- read discharge current setting
       dcset <value>        -- set discharge current

       vread                -- read DC voltage
       cread                -- read DC current
       acvread              -- read AC voltage

       charge <value>       -- set direction charge battery
       discharge <value>    -- set direction discharge battery
       dirread              -- read direction 0:charge,1:discharge

       tempread             -- read power supply temperature
       typeread             -- read power supply type
       statusread           -- read power supply status
       dump                 -- dump some common hardware values, hardware-type, build-date, mcu1/2 software-version
       faultread            -- read power supply fault status

       can_up               -- start can bus
       can_down             -- shut can bus down

       init_mode            -- init BIC-2200 bi-directional battery mode and eeprom write disable

       <value> = amps oder volts * 100 --> 25,66V = 2566 


# Configuration file **Under Construction !!!** 

Configuration File: bic2mqtt.ini

## Section [ALL]

|key                         | default value           | description                 |
|----------------------------|-------------------------|---------------------------- |
| TraceLevel                 | def:info                | possible levels: debug,info |
| TraceFilePath              | def:""                  | trace/log to file

## Section [MQTT]

|key                         | default value           | description             |
|----------------------------|-------------------------|------------------------ |
|BrokerIpAdr                 | def:"127.0.0.1"         | Broker IP-Address       |               
|BrokerAccUser               | def:""                  | Broker Account User     |
|BrokerAccUser               | def:""                  | Broker Account Password |
|TopicMain                   | def: haus/power/bat     | main topic              |


## Section [Device] 

|key                         | default value           | description   |
|----------------------------|-------------------------|-------------- |
|ChargeVoltage               | def:2750 volt*100       |               |
|DischargeVoltage            | def:2520 volt*100       |               | 
|MaxChargeCurrent            | def:3500 volt*100       |               |
|MaxDischargeCurrent         | def:2600 volt*100       |               |


## Section [BAT_0]

 - under construction

## Section [CHARGE_CONTROL]

To control charging and discharging with this app. 

|key                         | default value           | description   |
|----------------------------|-------------------------|-------------- |
|Id/X/Enable                  | def:1                   | >0 local charge control is enabled |
|Id/X/TopicPower             | ""                       | subscribe topic for grid power values from the smart meter  <0:power to public-grid, >0 power-consumption from public.grid|
|Id/X/TimeSliceCalcSec       | def:12 [s]               | time slice for each calculation loop |

--------

# MQTT Topics

- \<main-app> can be configured in ini-file

**Under Construction !!!** 

|pub/sub   | topic                   | payload     | description   |
|----------|-------------------------|-------------|-------------- |
|pub | "<main-app>/inv/\<id>/info      |             | json inverter hardware info, eg. version
|pub | \<main-app>/inv/\<id>/state       |             | json inverter states
|sub | \<main-app>/inv/\<id>/state/set   | [0,1]       | set inverter operating mode 1:on else off
|pub | \<main-app>/inv/\<id>/charge      |             | 
|pub | \<main-app>/inv/\<id>/fault       |             | json fault states of the inverter
|sub | \<main-app>/inv/\<id>/charge/set  | {"var":[chargeA,chargeP],"val":[ampere or power]} |
|pub | \<main-app>/sys/state/lwt       | [offline,running] | mqtt last will |
|sub | ini file: [CHARGE_CONTROL]Id/X/TopicPower | value [W] | Charge control: incoming grid power values as a raw value [W]|
|sub | \<main-app>/inv/\<id>/control/set |  [0,1]    |  start stop charge-control, charging will be stoped on each toggle |

        
# Examples        
Example code to control battery charging and discharging depending on the electricity meter. 

**All scripts are without any warranty. Use at your own risk**
