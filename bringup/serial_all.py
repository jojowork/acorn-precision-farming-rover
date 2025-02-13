import serial
import time


# sudo sh -c "echo performance > /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
# cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
# 1500000

port_names = ('/dev/ttySC0', '/dev/ttySC1', '/dev/ttySC2', '/dev/ttySC3')

BAUD = 115200

port_list = []

for name in port_names:
    port_list.append(serial.Serial(name, BAUD, timeout=0.1))

for port in port_list:
    port.flushInput()
    port.flushOutput()

while True:
    time.sleep(0.01)
    for port in port_list:
        port.write(b'r vbus_voltage\n')
    time.sleep(0.01)
    for port in port_list:
        try:
            print("{} {}".format(port.name, port.readline()))
            port.flushInput()
            port.flushOutput()
        except KeyboardInterrupt:
            break
        except:
            print("SERIAL EXCEPTION")
for port in port_list:
    port.close()
