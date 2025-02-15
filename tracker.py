'''
The Tracker uses the PidController to generate commands for the telescope.
You tell the Tracker where to point the telescope, and it will drive the
telescope to that location.
'''

import math
import time

import nexstar
import util

class PidController:
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

class Tracker:
    '''Run a PidController for each axis, and drive the telescope to point at targets.'''
    def __init__(self, telescope, kp, ki, kd, altaz_mode):
        '''
        If altaz_mode = True, we'll track in alt/az coordinates.
        If altaz_mode = False, we'll track in ra/dec coordinates.
        '''
        self.telescope = telescope
        self.azm_or_ra_controller = PidController(kp, ki, kd)
        self.alt_or_dec_controller = PidController(kp, ki, kd)
        self.stopped=False
        self.altaz_mode = altaz_mode

    def set_gains(self, kp, ki, kd):
        '''Set controller gains.'''
        self.azm_or_ra_controller.set_gains(kp, ki, kd)
        self.alt_or_dec_controller.set_gains(kp, ki, kd)

    def stop(self):
        '''Stop the telescope.'''
        if not self.stopped:
            self.telescope.slew_azmalt(0, 0)
            self.telescope.set_tracking_mode(nexstar.TrackingMode.OFF)
            self.stopped = True
        self.azm_or_ra_controller.reset()
        self.alt_or_dec_controller.reset()

    def go(self, target_azm_or_ra, target_alt_or_dec):
        '''
        Set the telescope slew rate to move towards the given position.
        This should be called on every control cycle (unless the tracker is stopped).
        '''
        self.stopped = False
        if self.altaz_mode:
            actual_azm_or_ra, actual_alt_or_dec = self.telescope.get_precise_azm_alt()
        else:
            actual_azm_or_ra, actual_alt_or_dec = self.telescope.get_precise_ra_dec()
        actual_azm_or_ra = util.wrap_rad(actual_azm_or_ra, target_azm_or_ra-math.pi)
        actual_alt_or_dec = util.wrap_rad(actual_alt_or_dec, target_alt_or_dec-math.pi)
        slew_rate_azm_or_ra = self.azm_or_ra_controller.control(target_azm_or_ra, actual_azm_or_ra)
        slew_rate_alt_or_dec = self.alt_or_dec_controller.control(target_alt_or_dec, actual_alt_or_dec)

        if abs(slew_rate_azm_or_ra) > 4/180*math.pi:
            self.azm_or_ra_controller.reset()
        if abs(slew_rate_alt_or_dec) > 4/180*math.pi:
            self.alt_or_dec_controller.reset()

        if self.altaz_mode:
            self.telescope.slew_azmalt(slew_rate_azm_or_ra, slew_rate_alt_or_dec)
        else:
            self.telescope.slew_radec(slew_rate_azm_or_ra, slew_rate_alt_or_dec)
