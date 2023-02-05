# Happy Hare ERCF Software
# Driver for encoder that supports movement measurement and runout/clog detection
#
# Copyright (C) 2022  moggieuk#6538 (discord)
#
# Based on:
# Original Enraged Rabbit Carrot Feeder Project  Copyright (C) 2021  Ette
# Generic Filament Sensor Module                 Copyright (C) 2019  Eric Callahan <arksine.code@gmail.com>
# Filament Motion Sensor Module                  Copyright (C) 2021 Joshua Wherrett <thejoshw.code@gmail.com>
#
# (\_/)
# ( *,*)
# (")_(") ERCF Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time
from . import pulse_counter

class ErcfEncoder:
    CHECK_MOVEMENT_TIMEOUT = .250

    RUNOUT_DISABLED = 0
    RUNOUT_STATIC = 1
    RUNOUT_AUTOMATIC = 2

    VARS_ERCF_CALIB_CLOG_LENGTH = "ercf_calib_clog_length" # in ercf_vars.cfg variables because it is autotuned

    def __init__(self, config):
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        encoder_pin = config.get('encoder_pin')

        # For counter functionality
        self.sample_time = config.getfloat('sample_time', 0.1, above=0.)
        self.poll_time = config.getfloat('poll_time', 0.0001, above=0.)
        self.resolution = config.getfloat('encoder_resolution', above=0.) # Must be calibrated by user
        self._last_time = None
        self._counts = self._last_count = 0
        self._encoder_steps = self.resolution
        self._counter = pulse_counter.MCU_counter(self.printer, encoder_pin, self.sample_time, self.poll_time)
        self._counter.setup_callback(self._counter_callback)
        self._movement = False

        # For clog/runout functionality
        self.extruder_name = config.get('extruder', None)
        self.desired_headroom = config.getfloat('headroom', 5., above=0.)
        self.average_samples = config.getint('average_samples', 4, minval=1)
        self.next_calibration_point = self.calibration_length = config.getfloat('calibration_length', 100., minval=50.)
        self.detection_length = config.getfloat('detection_length', 8., above=2.)
        self.event_delay = config.getfloat('event_delay', 3., above=0.)
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.runout_gcode = gcode_macro.load_template(config, 'runout_gcode', '_ERCF_ENCODER_RUNOUT')
        self.insert_gcode = gcode_macro.load_template(config, 'insert_gcode', '_ERCF_ENCODER_INSERT')
        self._enabled = True
        self.min_event_systime = self.reactor.NEVER
        self.extruder = self.estimated_print_time = None
        self.filament_detected = False
        self.detection_mode = self.RUNOUT_STATIC
        self.last_extruder_pos = 0.
        self._logger = None

        # Register event handlers
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('klippy:connect', self._handle_connect)
        self.printer.register_event_handler('idle_timeout:printing', self._handle_printing)
        self.printer.register_event_handler('idle_timeout:ready', self._handle_not_printing)
        self.printer.register_event_handler('idle_timeout:idle', self._handle_not_printing)

    def _handle_connect(self):
        self.variables = self.printer.lookup_object('save_variables').allVariables
        self.detection_length = self.filament_runout_pos = self.min_headroom = self.variables.get(self.VARS_ERCF_CALIB_CLOG_LENGTH, self.detection_length)

    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2. # Don't process events too early
        self.extruder = self.printer.lookup_object(self.extruder_name)
        self.estimated_print_time = self.printer.lookup_object('mcu').estimated_print_time
        self._reset_filament_runout_params()
        self._extruder_pos_update_timer = self.reactor.register_timer(self._extruder_pos_update_event)

    def _handle_printing(self, print_time):
        self.reactor.update_timer(self._extruder_pos_update_timer, self.reactor.NOW) # Enabled

    def _handle_not_printing(self, print_time):
        self.reactor.update_timer(self._extruder_pos_update_timer, self.reactor.NEVER) # Disabled

    def _get_extruder_pos(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        print_time = self.estimated_print_time(eventtime)
        return self.extruder.find_past_position(print_time)

    # Called periodically to check filament movement 
    def _extruder_pos_update_event(self, eventtime):
        if not self._enabled: return
        extruder_pos = self._get_extruder_pos(eventtime)

        # First lets see if we got encoder movement since last invocation
        if self._movement and self.extruder is not None:
            self._movement = False
            self.filament_runout_pos = extruder_pos + self.detection_length

        if extruder_pos >= self.next_calibration_point:
            if self.next_calibration_point > 0:
                logging.info("MOGGIE TEMP: past calibration point. Recalibrating detection_length...")
                self._update_detection_length()
            self.next_calibration_point = extruder_pos + self.calibration_length
        if self.filament_runout_pos - extruder_pos < self.min_headroom:
            self.min_headroom = self.filament_runout_pos - extruder_pos
            logging.info("MOGGIE TEMP: new min_headroom: %.1f" % self.min_headroom)
            if self._logger and self.min_headroom < self.desired_headroom:
                if self.detection_mode == self.RUNOUT_AUTOMATIC:
                    self._logger("Automatic clog detection: new min_headroom: %.1f" % self.min_headroom)
                elif self.detection_mode == self.RUNOUT_STATIC:
                    self._logger("Warning: Only %.1fmm of headroom to clog/runout" % self.min_headroom)
        self._handle_filament_event(extruder_pos < self.filament_runout_pos)
        self.last_extruder_pos = extruder_pos
        return eventtime + self.CHECK_MOVEMENT_TIMEOUT

    def _reset_filament_runout_params(self, eventtime=None):
        logging.info("MOGGIE TEMP: _reset_filament_runout_params")
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        self.last_extruder_pos = self._get_extruder_pos(eventtime)
        self.filament_runout_pos = self.last_extruder_pos + self.detection_length
        self.min_headroom = self.detection_length

    # Called periodically to tune the clog detection length
    def _update_detection_length(self, increase_only=False):
        if not self._enabled: return
        logging.info("MOGGIE TEMP: trace... _update_detection_length mode=%d, increase_only=%s, min_headroom=%.1f, headroom=%.1f" % (self.detection_mode, increase_only, self.min_headroom, self.desired_headroom))
        logging.info("MOGGIE TEMP: Status: %s" % self.get_status(0))
        if self.detection_mode != self.RUNOUT_AUTOMATIC:
            return
        current_detection_length = self.detection_length
        if self.min_headroom < self.desired_headroom:
            # Maintain headroom
            extra_length = min((self.desired_headroom - self.min_headroom), self.desired_headroom)
            self.detection_length += extra_length
            logging.info("MOGGIE TEMP: maintaining headroom by adding %.1f to detection_length, new detection_length=%.1f" % (extra_length, self.detection_length))
        elif not increase_only:
            # Average down
            sample = self.detection_length - (self.min_headroom - self.desired_headroom)
            self.detection_length = ((self.average_samples * self.detection_length) + self.desired_headroom - self.min_headroom) / self.average_samples
            logging.info("MOGGIE TEMP: averaging down with %.1f sample, new detection_length=%.1f" % (sample, self.detection_length))
        else:
            logging.info("MOGGIE TEMP: ignoring average down request")
            return

        self.min_headroom = self.detection_length
        if round(self.detection_length, 1) != round(current_detection_length, 1): # Persist if significant
            if self._logger:
                self._logger("Automatic clog detection: reset detection_length to %.1fmm" % self.min_headroom)
            self.set_clog_detection_length(self.detection_length)

    # Called to see if state update requires callback notification
    def _handle_filament_event(self, filament_detected):
        if self.filament_detected == filament_detected:
            return
        self.filament_detected = filament_detected
        logging.info("MOGGIE TEMP: RUNOUT EVENT filament_detected: %s **************************************************************" % filament_detected)
        eventtime = self.reactor.monotonic()
        if eventtime < self.min_event_systime or self.detection_mode == self.RUNOUT_DISABLED or not self._enabled:
            return
        is_printing = self.printer.lookup_object("idle_timeout").get_status(eventtime)["state"] == "Printing"
        if filament_detected:
            if not is_printing and self.insert_gcode is not None:
                # Insert detected
                self.min_event_systime = self.reactor.NEVER
                logging.info("Encoder Sensor %s: insert event detected, Time %.2f" % (self.name, eventtime))
                self.reactor.register_callback(self._insert_event_handler)
        else:
            if is_printing and self.runout_gcode is not None:
                # Runout detected
                self.min_event_systime = self.reactor.NEVER
                logging.info("Encoder Sensor %s: runout event detected, Time %.2f" % (self.name, eventtime))
                self.reactor.register_callback(self._runout_event_handler)

    def _runout_event_handler(self, eventtime):
        self._exec_gcode(self.runout_gcode)

    def _insert_event_handler(self, eventtime):
        self._exec_gcode(self.insert_gcode)

    def _exec_gcode(self, template):
        try:
            self.gcode.run_script(template.render())
        except Exception:
            logging.exception("Script running error")
        self.min_event_systime = self.reactor.monotonic() + self.event_delay

    def get_clog_detection_length(self):
        return self.detection_length

    def set_clog_detection_length(self, clog_length):
        clog_length = max(clog_length, 2.)
        self.detection_length = clog_length
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%.1f" % (self.VARS_ERCF_CALIB_CLOG_LENGTH, clog_length))
        logging.info("MOGGIE TEMP: Clog detection length changed (and persisted) as %.1f mm" % round(clog_length, 1))
        self._reset_filament_runout_params()

    def set_mode(self, mode):
        self.detection_mode = mode

    def set_logger(self, log):
        self._logger = log

    def enable(self):
        self._reset_filament_runout_params()
        self._enabled = True

    def disable(self):
        self._update_detection_length(increase_only=True)
        self._enabled = False

    def is_enabled(self):
        return self._enabled

    # Callback for MCU_counter
    def _counter_callback(self, time, count, count_time):
        if self._last_time is None:  # First sample
            self._last_time = time
        elif count_time > self._last_time:
            self._last_time = count_time
            new_counts = count - self._last_count
            self._counts += new_counts
            self._movement = (new_counts > 0)
        else:  # No counts since last sample
            self._last_time = time
        self._last_count = count

    def get_counts(self):
        return self._counts

    def get_distance(self):
        return (self._counts / 2.) * self._encoder_steps

    def set_distance(self, new_distance):
        self._counts = int((new_distance / self._encoder_steps) * 2.)

    def reset_counts(self):
        self._counts = 0.

    def get_status(self, eventtime):
        return {
                'detetion_length': round(self.detection_length, 1),
                'min_headroom': round(self.min_headroom, 1),
                'headroom': round(self.filament_runout_pos - self.last_extruder_pos, 1)
                }

def load_config_prefix(config):
    return ErcfEncoder(config)

