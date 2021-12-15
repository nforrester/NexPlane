'''
The Tracker uses the PidController to generate commands for the telescope.
You tell the Tracker where to point the telescope, and it will drive the
telescope to that location.
'''

import math
import time

import nexstar
import util

class PidController(object):
    '''Does exactly what it says on the tin.'''
    def __init__(self, kp, ki, kd):
        '''Create a new PidController with the specified gains.'''
        self.set_gains(kp, ki, kd)

    def set_gains(self, kp, ki, kd):
        '''Set the gains and reset the controller.'''
        self.kp = kp
        self.ki = ki
        self.kd = kd

        self.reset()

    def reset(self):
        '''Reset the controller.'''
        self.i_error = 0.0
        self.last_error = None
        self.last_time = None

    def control(self, desired, actual):
        '''
        Given a desired position and an actual position for the current
        control step, return a command output that drives the actual
        position towards the desired position.
        '''
        error = desired - actual
        now = time.time()

        output = self.kp * error

        if self.last_time is not None:
            dt = now - self.last_time
            self.i_error += error * dt
            d_error = (error - self.last_error) / dt

            output += self.ki * self.i_error
            output += self.kd * d_error

        self.last_error = error
        self.last_time = now

        return output

class Tracker(object):
    '''Run a PidController for each axis, and drive the telescope to point at targets.'''
    def __init__(self, telescope, kp, ki, kd):
        self.telescope = telescope
        self.azm_controller = PidController(kp, ki, kd)
        self.alt_controller = PidController(kp, ki, kd)
        self.stopped=False

    def set_gains(self, kp, ki, kd):
        '''Set controller gains.'''
        self.azm_controller.set_gains(kp, ki, kd)
        self.alt_controller.set_gains(kp, ki, kd)

    def stop(self):
        '''Stop the telescope.'''
        if not self.stopped:
            self.telescope.slew(0, 0)
            self.telescope.set_tracking_mode(nexstar.TrackingMode.OFF)
            self.stopped = True
        self.azm_controller.reset()
        self.alt_controller.reset()

    def go(self, target_azm, target_alt):
        '''
        Set the telescope slew rate to move towards the given position.
        This should be called on every control cycle (unless the tracker is stopped).
        '''
        self.stopped = False
        actual_azm, actual_alt = self.telescope.get_precise_azm_alt()
        actual_azm = util.wrap_rad(actual_azm, target_azm-math.pi)
        slew_rate_azm = self.azm_controller.control(target_azm, actual_azm)
        slew_rate_alt = self.alt_controller.control(target_alt, actual_alt)

        if abs(slew_rate_azm) > 4/180*math.pi:
            self.azm_controller.reset()
        if abs(slew_rate_alt) > 4/180*math.pi:
            self.alt_controller.reset()

        self.telescope.slew(slew_rate_azm, slew_rate_alt)
