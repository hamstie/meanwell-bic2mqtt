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


# Configuration file for the MQTT-Bridge

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

Create a SOC list of the battery device to convert voltage to percent

|key                          | default value  | description   |
|-----------------------------|----------------|-------------- |
|Cap2V/X                      |                | Capacity to Voltage Value to create a SOC list of the bat device e.g Cap2V/45=20 means cap45% is 20V SOC Volatge |


## Section [CHARGE_CONTROL]

To control charging and discharging with this app. 

|key                          | default value           | description   |
|-----------------------------|-------------------------|-------------- |
|Id/X/Type                    | def:"PID"               | possible charger: pid, winter, none |
|Id/X/TopicPower              | ""                      | subscribe topic for grid power values from the smart meter  <0:power to public-grid, >0 power-consumption from public.grid|
|Id/X/TimeSliceCalcSec        | def:12 [s]              | time slice for each calculation loop (not used yet)       |
|Id/X/DischargeBlockTimeSec   | def: 60[s]              | skip short discharge bursts                               |
|Id/X/ChargeTol               | def: 10[W]              | don't set new charge value if the running one is nearby   |
|Id/0/LoopGain                | def:0.5                 | regulator loop gain (only for the simple charger )        |
|Id/X/Pid/MaxChargePower      | def:400 [W]             | max charge power value (relative for each step) [W]       |
|Id/X/Pid/MaxDischargePower   | def:-400 [W]            | max discharge power  (relative for each step) [W]         |
|  PID Charge-Control         |                         |                                                           |
|Id/X/Pid/P                   | def:1.0  [0..1.0]       | P-Factor                                                  |
|Id/X/Pid/I                   | def:0.0  [0..1.0]       | I-Factor leave it zero for simple config                  |
|Id/X/Pid/D                   | def:0.0  [0..1.0]       | D-Factor leave it zero for simple config                  |
|  Winter Charge Control      |                         | |
|Id/X/Id/X/Winter/ChargeP     | def:200W [VA]           | const winter charge power for charging/discharging        | 
|Id/X/Id/X/Winter/TempMin     | def:10 [C]              | |
|Id/X/Id/X/Winter/CapMin      | def:20 [%]              | |
|Id/X/Id/X/Winter/CapMax      | def:50 [%]              | |
|  Charge Profile (only for pid) |                      | |
|Id/X/Profile/Hour/h/MaxChargePower    | def:0 [W]      | Charge profiles, per hour [0..23]    |
|Id/X/Profile/Hour/h/MaxDischargePower |def:0 [W]       | Discharge profiles, per hour [0..23] |
|Id/X/Profile/Hour/h/GridOffsetPower   |def:0 [W]       | Grid(Smart-Meter) offset , per hour [0..23] |

## Section [SURPLUS_SWITCH]

To switch power consumer if surplus power is available

|key                          | default value           | description   |
|-----------------------------|-------------------------|-------------- |
| SwitchDelaySec               |def:40 [s]              |[s] Delay between each switch action (on/off) to ensure proper grid power response for new decisions.|
|Id/X/switch/Y/Name            | | name to debug each switch will be switched on if surplus reached the threshold |
|Id/X/switch/Y/Topic           |                              | switch topic payload: [0,1] |
|Id/X/switch/Y/SurplusMinP     | def:0[W] disabled            | Min. power to switch on the switch |
|Id/X/switch/Y/MinDurationMin  |def:5[min]                    | Min. time the switch is on |
|Id/X/switch/Y/MaxDurationMax  |(def:-1 [min] endless)        | Max. time the switch is on | 
	



--------

# MQTT Topics

- \<main-app> can be configured in ini-file
- hot config is possible

|pub/sub   | topic                   | payload     | description   |
|----------|-------------------------|-------------|-------------- |
|pub | "<main-app>/inv/\<id>/info      |             | json inverter hardware info, eg. version
|pub | \<main-app>/inv/\<id>/state       |             | json inverter states
|sub | \<main-app>/inv/\<id>/state/set   | [0,1]       | set inverter operating mode 1:on else off
|pub | \<main-app>/inv/\<id>/charge      |             | 
|pub | \<main-app>/inv/\<id>/fault       |             | json fault states of the inverter
|sub | \<main-app>/inv/\<id>/charge/set  | {"var":[chargeA,chargeP],"val":[ampere or power]} | publish "var":"cfgReload" to reload configuration from ini-file 
|pub | \<main-app>/sys/state/lwt       | [offline,running] | mqtt last will |
|sub | ini file: [CHARGE_CONTROL]Id/X/TopicPower | value [W] | Charge control: incoming grid power values as a raw value [W]|
|sub | \<main-app>/inv/\<id>/control/set |  [0,1]    |  start stop charge-control, charging will be stoped on each toggle |


# Deploy
 - Configure bic2mqtt.ini
   - Configure your battery profile for SOC values
   - Configure Device charge and discharge values for your bic device (be careful !!)
   - Disable charge controller [CHARGE_CONTROL] Id/0/Type="none"
 - Start bic2mqtt.py and test the limit of charging/discharging:publish some charge values by hand (topic:<main-app>/inv/0/charge/set  e.g. {"var":"chargeP","val":30} )
 - After all enable the pid controller and configure the P-I-D parameter (hot config is possible with cfgReload)
   
# Examples        
Example code to control battery charging and discharging with cbic2200.py depending on the electricity smart-meter grid-power.

**All scripts are without any warranty. Use at your own risk**
