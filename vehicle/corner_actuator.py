"""
*********************************************************************
                     This file is part of:
                       The Acorn Project
             https://wwww.twistedfields.com/research
*********************************************************************
Copyright (c) 2019-2021 Taylor Alexander, Twisted Fields LLC
Copyright (c) 2021 The Acorn Project contributors (cf. AUTHORS.md).

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*********************************************************************
"""

"""
Control one corner of the Acorn robot via the Odrive motor controller.
"""
import time
import math
import sys
import random
import collections

import fibre
import odrive_manager
import fake_odrive_manager
from odrive.enums import *
from odrive.utils import _VT100Colors
import model
from model import CORNER_NAMES
import async_odrive

THERMISTOR_ADC_CHANNEL = 3
_ADC_PORT_STEERING_POT = 5
_ADC_PORT_STEERING_HOME = 6


_VCC = 3.3
_VOLTAGE_MIDPOINT = _VCC/2
_POT_VOLTAGE_LOW = _VCC/6
_POT_VOLTAGE_HIGH = 5*_VCC/6
_HOMING_VELOCITY_STEERING_COUNTS = 800
_HOMING_POT_DEADBAND_VOLTS = 0.2
_HOMING_POT_HALF_DEADBAND_V = _HOMING_POT_DEADBAND_VOLTS/2
_FAST_POLLING_SLEEP_S = 0.01
_SLOW_POLLING_SLEEP_S = 0.5
_ODRIVE_CONNECT_TIMEOUT = 75
_MAX_HOMING_ATTEMPTS = 4
_ZERO_VEL_COUNTS_THRESHOLD = 2

try:
    CTRL_MODE_POSITION_CONTROL
    CTRL_MODE_VELOCITY_CONTROL
except:
    print("WARNING: potentially incompatible odrive library version!")
    CTRL_MODE_POSITION_CONTROL = CONTROL_MODE_POSITION_CONTROL
    CTRL_MODE_VELOCITY_CONTROL = CONTROL_MODE_VELOCITY_CONTROL


COMMAND_VALUE_MINIMUM = 0.001

COUNTS_PER_REVOLUTION = 9797.0
COUNTS_PER_REVOLUTION_NEW_STEERING = COUNTS_PER_REVOLUTION * 520.0/2000.0

ESTOP_PIN = 6

OdriveConnection = collections.namedtuple(
    'OdriveConnection', 'name serial path enable_steering enable_traction')

# class PowerSample:
#     def init(self, volts, amps, duration):
#
#
# class PowerTracker:
#     def init(self, interval):
#         self.recent_samples = []
#         self.collapse_samples_interval = interval


class CornerActuator:

    def __init__(self, serial_number=None, name=None, path=None, GPIO=None,
                 connection_definition=None, enable_steering=True, enable_traction=True,
                 simulated_hardware=False, disable_steering_limits=False):
        if connection_definition:
            serial_number = connection_definition.serial
            name = connection_definition.name
            path = connection_definition.path
        if serial_number and not isinstance(serial_number, str):
            raise ValueError("serial_number must be of type str but got type: {}".format(
                type(serial_number)))

        self.GPIO = GPIO
        if simulated_hardware:
            self.odrv0 = fake_odrive_manager.FakeOdriveManager(
                path=path, serial_number=serial_number).find_odrive()
        else:
            self.odrv0 = odrive_manager.OdriveManager(
                path=path, serial_number=serial_number).find_odrive()

        self.name = name
        self.enable_steering = enable_steering
        self.enable_traction = enable_traction
        self.steering_axis = self.odrv0.axis0
        self.traction_axis = self.odrv0.axis1
        self.steering_error = self.steering_axis.error
        self.traction_error = self.traction_axis.error
        self.steering_initialized = False
        self.traction_initialized = False
        self.position = 0.0
        self.velocity = 0.0
        self.voltage = 0.0
        self.home_position = 0
        self.steering_flipped = False
        self.has_thermistor = False
        self.temperature_c = None
        self.zero_vel_timestamp = None
        self.disable_steering_limits = disable_steering_limits
        self.simulated_hardware = simulated_hardware
        self.encoder_estimates = (0.0,0.0,0.0,0.0)
        self.errors = 0
        self.rotation_sensor_val = None

        if self.simulated_hardware:
            self.odrv0.vbus_voltage = 45.5 + random.random() * 2.0

    def enable_thermistor(self):
        self.has_thermistor = True

    def idle_wait(self):
        while self.odrv0.axis1.current_state != AXIS_STATE_IDLE:
            toggling_sleep(self.GPIO, 0.1)

    def print_errors(self, clear_errors=False):
        self.dump_errors(clear_errors)
        gpio_toggle(self.GPIO)
        if clear_errors:
            self.position = 0
            self.velocity = 0

    def sample_home_sensor(self):
        gpio_toggle(self.GPIO)
        return self.odrv0.get_adc_voltage(_ADC_PORT_STEERING_HOME) < _VOLTAGE_MIDPOINT

    def sample_steering_pot(self):
        gpio_toggle(self.GPIO)
        self.rotation_sensor_val = self.odrv0.get_adc_voltage(_ADC_PORT_STEERING_POT)
        return self.rotation_sensor_val

    def initialize_traction(self):
        gpio_toggle(self.GPIO)
        self.odrv0.axis1.controller.vel_integrator_current = 0
        self.odrv0.axis1.controller.config.vel_integrator_gain = 0.5
        self.odrv0.axis1.controller.config.control_mode = CTRL_MODE_VELOCITY_CONTROL
        self.odrv0.axis1.requested_state = AXIS_STATE_CLOSED_LOOP_CONTROL
        self.traction_initialized = True

    def check_steering_limits(self, rotation_sensor_val=None):
        if rotation_sensor_val is None:
            rotation_sensor_val = self.sample_steering_pot()
        if self.disable_steering_limits:
            print("{} steering sensor {}".format(
                list(CORNER_NAMES)[self.name], rotation_sensor_val))
        if rotation_sensor_val < _POT_VOLTAGE_LOW or rotation_sensor_val > _POT_VOLTAGE_HIGH:
            if self.disable_steering_limits:
                print("WARNING POTENTIOMETER VOLTAGE OUT OF RANGE DAMAGE " +
                      "MAY OCCUR: {}".format(rotation_sensor_val))
            else:
                raise ValueError(
                    "POTENTIOMETER VOLTAGE OUT OF RANGE: {}".format(rotation_sensor_val))

    def recover_from_estop(self):
        gpio_toggle(self.GPIO)
        self.odrv0.axis0.controller.config.control_mode = CTRL_MODE_POSITION_CONTROL
        self.odrv0.axis0.requested_state = AXIS_STATE_CLOSED_LOOP_CONTROL
        self.initialize_traction()

    def initialize_steering(self, steering_flipped=False, skip_homing=False):
        gpio_toggle(self.GPIO)
        self.check_steering_limits()
        self.enable_steering_control()

        self.steering_flipped = steering_flipped

        self.home_position = self.odrv0.axis0.encoder.pos_estimate
        home_position = None
        if not skip_homing:

            last_home_sensor_val = self.sample_home_sensor()
            self.sample_steering_pot()
            print("Rotation sensor voltage: {}".format(self.rotation_sensor_val))
            gpio_toggle(self.GPIO)

            _HOMING_DISPLACEMENT_DEGREES = 50.0
            _HOMING_DISPLACEMENT_RADIANS = math.radians(
                _HOMING_DISPLACEMENT_DEGREES)

            _RETRY_OFFSET_DEGREES = 80

            position = _HOMING_DISPLACEMENT_DEGREES

            tick_time_s = 3
            last_tick_time = 0
            transitions = []
            attempts = 0
            this_position_failed = False
            position_checks = 0
            while True:
                if this_position_failed:
                    position_checks += 1
                    if position_checks > 1 + 360 / _RETRY_OFFSET_DEGREES:
                        raise RuntimeError("Exceeded max homing attempts.")
                    attempts = 0
                    this_position_failed = False
                    self.sample_steering_pot()
                    if self.rotation_sensor_val > _VOLTAGE_MIDPOINT:
                        offset = _RETRY_OFFSET_DEGREES
                    else:
                        offset = _RETRY_OFFSET_DEGREES * -1.0
                    if self.name == model.CORNER_NAMES['rear_left']:
                        self.home_position = self.home_position + offset * \
                            COUNTS_PER_REVOLUTION_NEW_STEERING / 360.0 * -1.0
                    else:
                        self.home_position = self.home_position + offset * \
                            COUNTS_PER_REVOLUTION / 360.0

                gpio_toggle(self.GPIO)
                time.sleep(_FAST_POLLING_SLEEP_S)
                gpio_toggle(self.GPIO)
                home_sensor_val = self.sample_home_sensor()

                self.sample_steering_pot()
                print("{} Home: {}, Rotation: {}".format(
                    list(CORNER_NAMES)[self.name], home_sensor_val, self.rotation_sensor_val))
                if home_sensor_val == True:
                    self.home_position = self.odrv0.axis0.encoder.pos_estimate
                    break
                if home_sensor_val != last_home_sensor_val:
                    transitions.append(self.odrv0.axis0.encoder.pos_estimate)
                    print(transitions)

                if time.time() - last_tick_time > tick_time_s:
                    last_tick_time = time.time()
                    position *= -1
                    if self.name == model.CORNER_NAMES['rear_left']:
                        pos_counts = self.home_position + position * \
                            COUNTS_PER_REVOLUTION_NEW_STEERING / 360.0 * -1.0
                    else:
                        pos_counts = self.home_position + position * COUNTS_PER_REVOLUTION / 360.0
                    self.odrv0.axis0.controller.move_to_pos(pos_counts)
                    attempts += 1
                    #self.odrv0.axis0.controller.pos_setpoint = pos_counts

                if len(transitions) > 4:
                    self.home_position = sum(transitions)/len(transitions)
                    break

                if attempts > _MAX_HOMING_ATTEMPTS:
                    this_position_failed = True
                    # raise RuntimeError("Exceeded max homing attempts.")
                try:
                    self.check_errors()
                except RuntimeError:
                    raise RuntimeError("ODrive threw error during homing.")

                last_home_sensor_val = home_sensor_val
                try:
                    self.check_steering_limits()
                except Exception as e:
                    raise e  # Finally will be called before the raise.
                finally:
                    self.stop_actuator()

        else:
            self.home_position = self.odrv0.axis0.encoder.pos_estimate
        self.odrv0.axis0.controller.move_to_pos(self.home_position)

        self.steering_initialized = True

    def enable_steering_control(self):
        self.odrv0.axis0.encoder.config.use_index = False
        self.odrv0.axis0.motor.config.direction = 1
        self.odrv0.axis0.motor.config.motor_type = 4  # MOTOR_TYPE_BRUSHED_VOLTAGE
        self.odrv0.axis0.motor.config.current_lim = 20.0
        gpio_toggle(self.GPIO)
        if self.name == model.CORNER_NAMES['rear_left']:
            self.odrv0.axis0.controller.config.vel_gain = 0.020
            self.odrv0.axis0.controller.config.pos_gain = -40
            self.odrv0.axis0.encoder.config.cpr = 520
            self.odrv0.axis0.controller.config.vel_ramp_rate = 80
            self.odrv0.axis0.trap_traj.config.vel_limit = 32000
            self.odrv0.axis0.trap_traj.config.accel_limit = 12000
            self.odrv0.axis0.trap_traj.config.decel_limit = 12000
            self.odrv0.axis0.trap_traj.config.A_per_css = 0
        else:
            self.odrv0.axis0.controller.config.vel_gain = 0.005
            self.odrv0.axis0.controller.config.pos_gain = -10
            self.odrv0.axis0.encoder.config.cpr = 2000
            self.odrv0.axis0.controller.config.vel_ramp_rate = 80
            self.odrv0.axis0.trap_traj.config.vel_limit = 16000
            self.odrv0.axis0.trap_traj.config.accel_limit = 6000
            self.odrv0.axis0.trap_traj.config.decel_limit = 6000
            self.odrv0.axis0.trap_traj.config.A_per_css = 0
        self.odrv0.axis0.encoder.config.mode = ENCODER_MODE_INCREMENTAL
        self.odrv0.axis0.requested_state = AXIS_STATE_ENCODER_INDEX_SEARCH

        gpio_toggle(self.GPIO)
        self.odrv0.axis0.encoder.config.use_index = False
        err_count = 0
        while True:
            self.odrv0.axis0.requested_state = AXIS_STATE_IDLE
            toggling_sleep(self.GPIO, _SLOW_POLLING_SLEEP_S)
            self.odrv0.axis0.controller.config.control_mode = CTRL_MODE_POSITION_CONTROL
            self.odrv0.axis0.requested_state = AXIS_STATE_CLOSED_LOOP_CONTROL
            self.odrv0.axis0.controller.vel_ramp_enable = True
            # self.odrv0.axis0.controller.config.vel_limit_tolerance = 2.5
            toggling_sleep(self.GPIO, _SLOW_POLLING_SLEEP_S)
            errors = self.odrv0.axis0.error
            if errors == 0:
                break
            if not self.odrv0.axis0.encoder.is_ready:
                raise RuntimeError(
                    "Could not initialize steering. Encoder is not ready.")

            # print("axis0.current_state: {}".format(self.odrv0.axis0.current_state))
            # self.odrv0.axis0.controller.vel_integrator_current = 0
            # self.odrv0.axis1.controller.vel_integrator_current = 0
            # self.odrv0.axis0.controller.vel_setpoint = 0
            # self.odrv0.axis1.controller.vel_setpoint = 0
            err_count += 1
            error_message = self.dump_errors(True)
            gpio_toggle(self.GPIO)
            if err_count > 3:
                raise RuntimeError(
                    "Could not initialize steering. Error was: {}".format(error_message))
            # TODO: Is this sleep time reasonable? Should also make it a variable.
            toggling_sleep(self.GPIO, 5)

    def check_errors(self, read_errors=True):
        # gpio_toggle(self.GPIO)
        if self.enable_traction and self.traction_axis.error:
            gpio_toggle(self.GPIO)
            self.dump_errors()
            self.update_voltage()
            raise RuntimeError("Odrive traction motor error state detected.")
        if self.enable_steering and self.steering_axis.error:
            gpio_toggle(self.GPIO)
            self.dump_errors()
            self.update_voltage()
            raise RuntimeError("Odrive steering motor error state detected.")

    def update_voltage(self):
        # gpio_toggle(self.GPIO)
        if self.simulated_hardware:
            self.odrv0.vbus_voltage = 45.5 + random.random() * 2.0
        self.voltage = self.odrv0.vbus_voltage
        self.ibus_0 = self.odrv0.axis0.motor.current_control.Ibus
        # gpio_toggle(self.GPIO)
        self.ibus_1 = self.odrv0.axis1.motor.current_control.Ibus

    def update_encoder_data(self):
        self.encoder_estimates = (self.steering_axis.encoder.pos_estimate,
                                    self.steering_axis.encoder.vel_estimate,
                                    self.traction_axis.encoder.pos_estimate,
                                    self.traction_axis.encoder.vel_estimate)
        # gpio_toggle(self.GPIO)

    def update_actuator_async(self, steering_pos_deg, drive_velocity, update_amps=False, update_volts=False, update_errors=False):
        gpio_toggle(self.GPIO)
        self.async_tracking_vals = []
        if self.steering_flipped:
            drive_velocity *= -1
        self.position = steering_pos_deg
        self.velocity = drive_velocity

        if self.simulated_hardware:
            return

        # Current prototype uses a different steering motor on rear left.
        if self.name == model.CORNER_NAMES['rear_left']:
            pos_counts = self.home_position + steering_pos_deg * \
                COUNTS_PER_REVOLUTION_NEW_STEERING / 360.0 * -1.0
        else:
            pos_counts = self.home_position + steering_pos_deg * COUNTS_PER_REVOLUTION / 360.0

        # Clear integrator if vehicle is sitting still.
        if abs(self.velocity) > _ZERO_VEL_COUNTS_THRESHOLD:
            self.zero_vel_timestamp = None
        else:
            if self.zero_vel_timestamp is None:
                self.zero_vel_timestamp = time.time()
            else:
                if time.time() - self.zero_vel_timestamp > 5.0:
                    async_odrive.set_value(self.traction_axis.controller, 'vel_integrator_current', 0.0)

        async_odrive.call_remote_function(self.steering_axis.controller, 'move_to_pos', pos_counts)
        time.sleep(0.0005)
        async_odrive.set_value(self.traction_axis.controller, 'vel_setpoint', self.velocity)
        time.sleep(0.0005)
        self.async_tracking_vals.append(async_odrive.request(self.steering_axis.encoder, 'pos_estimate'))
        time.sleep(0.0005)
        self.async_tracking_vals.append(async_odrive.request(self.steering_axis.encoder, 'vel_estimate'))
        time.sleep(0.0005)
        gpio_toggle(self.GPIO)
        self.async_tracking_vals.append(async_odrive.request(self.traction_axis.encoder, 'pos_estimate'))
        time.sleep(0.0005)
        self.async_tracking_vals.append(async_odrive.request(self.traction_axis.encoder, 'vel_estimate'))
        gpio_toggle(self.GPIO)
        if update_amps:
            time.sleep(0.0005)
            self.async_tracking_vals.append(async_odrive.request(self.steering_axis.motor.current_control, 'Ibus'))
            time.sleep(0.0005)
            self.async_tracking_vals.append(async_odrive.request(self.traction_axis.motor.current_control, 'Ibus'))
        if update_volts:
            time.sleep(0.0005)
            self.async_tracking_vals.append(async_odrive.call_remote_function(self.odrv0, 'get_adc_voltage', _ADC_PORT_STEERING_POT))
            time.sleep(0.0005)
            self.async_tracking_vals.append(async_odrive.request(self.odrv0, 'vbus_voltage'))
        if update_errors:
            time.sleep(0.0005)
            self.async_tracking_vals.append(async_odrive.request(self.steering_axis, 'error'))
            time.sleep(0.0005)
            self.async_tracking_vals.append(async_odrive.request(self.traction_axis, 'error'))
        gpio_toggle(self.GPIO)

    def retrieve_async_results(self, update_amps=False, update_volts=False, update_errors=False):

        if self.simulated_hardware:
            self.update_voltage()
            return True

        try:
            gpio_toggle(self.GPIO)
            time.sleep(0.0005)
            self.encoder_estimates = (async_odrive.retrieve_result(self.async_tracking_vals.pop(0)),
                                      async_odrive.retrieve_result(self.async_tracking_vals.pop(0)),
                                      async_odrive.retrieve_result(self.async_tracking_vals.pop(0)),
                                      async_odrive.retrieve_result(self.async_tracking_vals.pop(0)))
            if update_amps:
                self.ibus_0 = async_odrive.retrieve_result(self.async_tracking_vals.pop(0))
                self.ibus_1 = async_odrive.retrieve_result(self.async_tracking_vals.pop(0))
            if update_volts:
                self.check_steering_limits(async_odrive.retrieve_result(self.async_tracking_vals.pop(0)))
                self.voltage = async_odrive.retrieve_result(self.async_tracking_vals.pop(0))
            if update_errors:
                self.steering_error = async_odrive.retrieve_result(self.async_tracking_vals.pop(0))
                self.traction_error = async_odrive.retrieve_result(self.async_tracking_vals.pop(0))
            gpio_toggle(self.GPIO)
            return True
        except fibre.protocol.ChannelBrokenException:
            self.async_tracking_vals = []
            return False

    def update_actuator(self, steering_pos_deg, drive_velocity):
        #print("Update {}".format(self.name))
        # self.check_steering_limits()
        gpio_toggle(self.GPIO)
        if self.steering_flipped:
            drive_velocity *= -1
        self.position = steering_pos_deg
        self.velocity = drive_velocity
        # self.update_voltage()
        self.update_encoder_data()
        if self.name == model.CORNER_NAMES['rear_left']:
            pos_counts = self.home_position + steering_pos_deg * \
                COUNTS_PER_REVOLUTION_NEW_STEERING / 360.0 * -1.0
        else:
            pos_counts = self.home_position + steering_pos_deg * COUNTS_PER_REVOLUTION / 360.0

        self.odrv0.axis0.controller.move_to_pos(pos_counts)
        gpio_toggle(self.GPIO)

        if abs(self.velocity) > _ZERO_VEL_COUNTS_THRESHOLD:
            self.zero_vel_timestamp = None
        else:
            if self.zero_vel_timestamp is None:
                self.zero_vel_timestamp = time.time()
            else:
                if time.time() - self.zero_vel_timestamp > 5.0:
                    self.odrv0.axis1.controller.vel_integrator_current = 0
        self.odrv0.axis1.controller.vel_setpoint = self.velocity
        # self.check_errors()

    def slow_actuator(self, fraction):
        gpio_toggle(self.GPIO)
        if not (self.steering_initialized and self.traction_initialized):
            return
        position = fraction * self.position
        velocity = fraction * self.velocity
        if self.steering_flipped:
            velocity *= -1
        if math.fabs(self.position) < COMMAND_VALUE_MINIMUM:
            position = 0.0
        if math.fabs(self.velocity) < COMMAND_VALUE_MINIMUM:
            velocity = 0.0
        self.update_actuator(position, velocity)

    def stop_actuator(self):
        gpio_toggle(self.GPIO)
        self.odrv0.axis0.controller.vel_setpoint = 0
        self.odrv0.axis1.controller.vel_setpoint = 0

    def update_thermistor_temperature_C(self, adc_channel=THERMISTOR_ADC_CHANNEL):
        if not self.has_thermistor:
            return
        try:
            gpio_toggle(self.GPIO)
            value = self.odrv0.get_adc_voltage(adc_channel)
            resistance = 10000 / (3.3/value)
            self.temperature_c = self.thermistor_steinhart_temperature_C(
                resistance)
        except ZeroDivisionError:
            pass

    def thermistor_steinhart_temperature_C(self, r, Ro=10000.0, To=25.0, beta=3900.0):
        """ Via: https://learn.adafruit.com/thermistor/circuitpython """
        steinhart = math.log(r / Ro) / beta      # log(R/Ro) / beta
        steinhart += 1.0 / (To + 273.15)         # log(R/Ro) / beta + 1/To
        steinhart = (1.0 / steinhart) - 273.15   # Invert, convert to C
        return steinhart

    def dump_errors(self, clear=False):
        """A copy of dump_errors from odrive utils.py but with estop toggle added."""
        axes = [(name, axis) for name,
                axis in self.odrv0._remote_attributes.items() if 'axis' in name]
        gpio_toggle(self.GPIO)
        axes.sort()
        gpio_toggle(self.GPIO)
        for name, axis in axes:
            gpio_toggle(self.GPIO)
            print(name)

            # Flatten axis and submodules
            # (name, remote_obj, errorcode)
            module_decode_map = [
                ('axis', axis, errors.axis),
                ('motor', axis.motor, errors.motor),
                ('encoder', axis.encoder, errors.encoder),
                ('controller', axis.controller, errors.controller),
            ]

            # Module error decode
            try:
                for name, remote_obj, errorcodes in module_decode_map:
                    gpio_toggle(self.GPIO)
                    prefix = ' '*2 + name + ": "
                    if (remote_obj.error != errorcodes.ERROR_NONE):
                        gpio_toggle(self.GPIO)
                        print(prefix + _VT100Colors['red'] +
                              "Error(s):" + _VT100Colors['default'])
                        errorcodes_tup = [
                            (name, val) for name, val in errorcodes.__dict__.items() if 'ERROR_' in name]
                        for codename, codeval in errorcodes_tup:
                            gpio_toggle(self.GPIO)
                            if remote_obj.error & codeval != 0:
                                print("    " + codename)
                        if clear:
                            gpio_toggle(self.GPIO)
                            remote_obj.error = errorcodes.ERROR_NONE
                    else:
                        gpio_toggle(self.GPIO)
                        print(prefix + _VT100Colors['green'] +
                              "no error" + _VT100Colors['default'])
            except Exception as e:
                print("Exception in {}: {}".format(self.name, e))


def toggling_sleep(GPIO, duration):
    start = time.time()
    while time.time() - start < duration:
        gpio_toggle(GPIO)
        time.sleep(_FAST_POLLING_SLEEP_S)


def gpio_toggle(GPIO):
    GPIO.estop_state = not GPIO.estop_state
    GPIO.output(ESTOP_PIN, GPIO.estop_state)
