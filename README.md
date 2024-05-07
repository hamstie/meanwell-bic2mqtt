# meanwell-bic2mqtt
Tool to control Power Supplys from Mean Well via MQTT

was forked from https://github.com/stcan/meanwell-can-control <br>

Some new features:
 - possible to use cbic2200.py as a python module
 - can bus handling 
   - read-write-read check to decrease eeprom write cycles
   - write-read check to increase stability
   - read failues will be raised a Timout or RuntimeExeption
 - disabling eeprom write access (not tested yet, no available firmware-version)

Tested with the 24V Version BIC-2200-24-CAN<br>


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


# Configuration file 

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

 - create a SOC list of the battery device to convert voltage to percent

|key                          | default value  | description   |
|-----------------------------|----------------|-------------- |
|Cap2V/X                      |                | Capacity to Voltage Value to create a SOC list of the bat device e.g Cap2V/45=20 means cap45% is 20V SO Volatge |

 Cap2V/0     =19.00


## Section [CHARGE_CONTROL]

To control charging and discharging with this app. 

|key                          | default value           | description   |
|-----------------------------|-------------------------|-------------- |
|Id/X/Enable                  | def:1                   | >0 local charge control is enabled |
|Id/X/TopicPower              | ""                      | subscribe topic for grid power values from the smart meter  <0:power to public-grid, >0 power-consumption from public.grid|
|Id/X/TimeSliceCalcSec        | def:12 [s]              | time slice for each calculation loop (not used yet)       |
|Id/X/DischargeBlockHourStart | def:-1 [h]              | [0..23] start interval hour of day to block discharging   |
|Id/X/DischargeBlockHourStop  | def:-1 [h]              | [0..23] stop interval hour of day to block discharging    |
|Id/X/DischargeBlockTimeSec   | def: 60[s]              | skip short discharge bursts                               |
|Id/X/ChargePowerOffset       | def: 0[W]               | offset grid power for the calculation, move the zero point of power balance |
|Id/X/ChargeTol               | def: 10[W]              | don't set new charge value if the running one is nearby   |
|Id/0/LoopGain                | def:0.5                 | regulator loop gain (only for the simple charger )        |
|Id/X/Pid/MaxChargePower      | def:0 [W]               | max charge power value [W]                                |
|Id/X/Pid/MaxDischargePower   | def:0 [W]               | max discharge power [W]                                   |
|Id/X/Pid/P                   | def:1.0  [0..1.0]       | P-Factor                                                  |
|Id/X/Pid/I                   | def:0.0  [0..1.0]       | I-Factor leave it zero for simple config                  |
|Id/X/Pid/D                   | def:0.0  [0..1.0]       | D-Factor leave it zero for simple config                  |
|Id/X/Profile/Hour/h/MaxChargePower    | def:0 [W]      | Charge profiles, per hour [0..23]    |
|Id/X/Profile/Hour/h/MaxDischargePower |def:0 [W]       | Discharge profiles, per hour [0..23] |



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


# Deploy
 - Configure bic2mqtt.ini
   - Configure your battery profile for SOC values
   - Configure Device charge and discharge values for your bic device (be careful !!)
   - Disable charge controller [CHARGE_CONTROL] Id/0/Enabled=0
 - Start bic2mqtt.py and test the limit of charging/discharging
 - After all enable the pid controller and configure the P-I-D parameter of the pid
   
# Examples        
Example code to control battery charging and discharging with cbic2200.py depending on the electricity meter.

**All scripts are without any warranty. Use at your own risk**
