'''Graphical User Interface. Uses OpenGL/GLUT. Draws the sky using an equiangular projection.

The GUI runs in a separate thread, but the functions you use to feed it data or see what the
user wants can be called from the main thread. They just lock a mutex internally.
'''

import copy
import math
import numpy
import os
import scipy.spatial
import sys
import threading
import time

from abc import ABC, abstractmethod

from OpenGL import setPlatform
if os.environ.get('XDG_SESSION_TYPE') == 'wayland':
    setPlatform('glx')

import OpenGL.GL as gl
import OpenGL.GLU as glu
import OpenGL.GLUT as glut

import astropy.coordinates as coords

import util
from util import unwrap

from sbs1 import Airplane

class Exit(Exception):
    '''Thrown in the main thread when the GUI thread stops, probably because somebody closed the window.'''
    pass

Color = tuple[float, float, float]

class GuiLayer(ABC):
    '''
    A layer of information and behavior in the GUI.
    Several GuiLayers can be added to a Gui.

    Functions that mean to exchange data with the main thread should lock
    self.gui.iface_lock.
    '''
    def set_gui(self, gui: 'Gui') -> None:
        self.gui = gui

    @abstractmethod
    def pull_data_from_main_thread(self) -> None:
        '''
        Pull data from main-thread variables into gui-thread variables, in
        preparation for drawing.

        self.gui.iface_lock will be locked before this function is called.
        '''

    @abstractmethod
    def draw(self) -> None:
        '''Draw on the screen.'''

    @abstractmethod
    def handle_key(self, key: bytes, x: int, y: int) -> bool:
        '''
        A key has been pressed. If the key was meant for this layer,
        act upon it and return True. Otherwise return False, so the
        key can be offered to the next layer.
        '''

    @abstractmethod
    def handle_mouse(self, button: int, state: int, x: int, y: int) -> bool:
        '''
        The mouse has been clicked. If the click was meant for this layer,
        act upon it and return True. Otherwise return False, so the
        click can be offered to the next layer.
        '''

class CommWarningLayer(GuiLayer):
    '''Warn the user about communication failures with the telescope.'''
    def __init__(self) -> None:
        # If true, display a warning about a communication error.
        self.iface_warn_comm_failure = False

        self.warn_comm_failure = False

    def update_comm_failure(self, warn_comm_failure: bool) -> None:
        '''
        Call from the main thread to feed new data to the GUI so it can be displayed.
        See comments in __init__() for parameter definitions.
        '''
        self.gui.exit_if_dead()
        with self.gui.iface_lock:
            self.iface_warn_comm_failure = warn_comm_failure

    def pull_data_from_main_thread(self) -> None:
        self.warn_comm_failure = self.iface_warn_comm_failure

    def draw(self) -> None:
        if self.gui.black_and_white:
            if self.gui.white_bg:
                warn_color = (0.0, 0.0, 0.0)
            else:
                warn_color = (1.0, 1.0, 1.0)
        else:
            warn_color = (1.0, 0.0, 0.0)

        # Warn of a communication failure, if any.
        if self.warn_comm_failure:
            gl.glColor3f(*warn_color)
            x, y = self.gui.azm_alt_to_x_y(math.pi, 45/180*math.pi)
            warn_font = glut.GLUT_BITMAP_TIMES_ROMAN_24
            self.gui.draw_text(x-220, y+15, 'TELESCOPE COMMUNICATION FAILURE!', font=warn_font)
            self.gui.draw_text(x-140, y-15, 'PREVENT COLLISION!!!!', font=warn_font)

    def handle_key(self, key: bytes, x: int, y: int) -> bool:
        return False

    def handle_mouse(self, button: int, state: int, x: int, y: int) -> bool:
        return False

class GainReaderLayer(GuiLayer):
    '''Listen to user input setting control gains.'''
    def __init__(self, kp: float, ki: float, kd: float):
        self.iface_kp = kp          # Proportional gain for the Tracker's PidControllers. Can be adjusted by the user.
        self.iface_ki = ki          # Integral     gain for the Tracker's PidControllers. Can be adjusted by the user.
        self.iface_kd = kd          # Derivative   gain for the Tracker's PidControllers. Can be adjusted by the user.
        self.iface_gain_changes = 0 # How many times has the user changed the gains? Useful for knowing when to reset the PidControllers.

    def get_gains(self) -> tuple[float, float, float, int]:
        '''Return the current gain settings. See comments in __init__() for return value definitions.'''
        self.gui.exit_if_dead()
        with self.gui.iface_lock:
            return (self.iface_kp,
                    self.iface_ki,
                    self.iface_kd,
                    self.iface_gain_changes)

    def pull_data_from_main_thread(self) -> None:
        pass

    def draw(self) -> None:
        pass

    def handle_key(self, key: bytes, x: int, y: int) -> bool:
        '''RFTGYU adjust the PID controller gains up and down.'''
        if key in [b'r', b'R']:
            with self.gui.iface_lock:
                self.iface_kp += 0.01
                self.iface_gain_changes += 1
                return True
        elif key in [b'f', b'F']:
            with self.gui.iface_lock:
                self.iface_kp -= 0.01
                self.iface_gain_changes += 1
                return True
        elif key in [b't', b'T']:
            with self.gui.iface_lock:
                self.iface_ki += 0.01
                self.iface_gain_changes += 1
                return True
        elif key in [b'g', b'G']:
            with self.gui.iface_lock:
                self.iface_ki -= 0.01
                self.iface_gain_changes += 1
                return True
        elif key in [b'y', b'Y']:
            with self.gui.iface_lock:
                self.iface_kd += 0.01
                self.iface_gain_changes += 1
                return True
        elif key in [b'u', b'U']:
            with self.gui.iface_lock:
                self.iface_kd -= 0.01
                self.iface_gain_changes += 1
                return True
        return False

    def handle_mouse(self, button: int, state: int, x: int, y: int) -> bool:
        return False

class OffsetReaderLayer(GuiLayer):
    '''Listen to user input setting the pointing offset.'''
    def __init__(self) -> None:
        self.iface_azm_offset = 0.0
        self.iface_alt_offset = 0.0

    def get_offsets(self) -> tuple[float, float]:
        '''Return the current offsets.'''
        self.gui.exit_if_dead()
        with self.gui.iface_lock:
            return (self.iface_azm_offset, self.iface_alt_offset)

    def reset_offsets(self) -> None:
        '''Call from the main thread to reset the offsets to zero.'''
        self.gui.exit_if_dead()
        with self.gui.iface_lock:
            self.iface_azm_offset = 0.0
            self.iface_alt_offset = 0.0

    def pull_data_from_main_thread(self) -> None:
        pass

    def draw(self) -> None:
        pass

    def handle_key(self, key: bytes, x: int, y: int) -> bool:
        '''Read user inputs controlling the pointing offsets.'''
        small_motion = 0.1 / 180 * math.pi
        big_motion = 5 * small_motion

        # WASD or HJKL add manual offsets to the target location.
        # Capital letters make larger movements.
        if key == b'w' or key == b'k':
            with self.gui.iface_lock:
                self.iface_alt_offset += small_motion
                return True
        elif key == b'a' or key == b'h':
            with self.gui.iface_lock:
                self.iface_azm_offset -= small_motion
                return True
        elif key == b's' or key == b'j':
            with self.gui.iface_lock:
                self.iface_alt_offset -= small_motion
                return True
        elif key == b'd' or key == b'l':
            with self.gui.iface_lock:
                self.iface_azm_offset += small_motion
                return True
        elif key == b'W' or key == b'K':
            with self.gui.iface_lock:
                self.iface_alt_offset += big_motion
                return True
        elif key == b'A' or key == b'H':
            with self.gui.iface_lock:
                self.iface_azm_offset -= big_motion
                return True
        elif key == b'S' or key == b'J':
            with self.gui.iface_lock:
                self.iface_alt_offset -= big_motion
                return True
        elif key == b'D' or key == b'L':
            with self.gui.iface_lock:
                self.iface_azm_offset += big_motion
                return True
        # Q or O resets the offset.
        elif key in [b'q', b'Q', b'o', b'O']:
            with self.gui.iface_lock:
                self.iface_azm_offset = 0.0
                self.iface_alt_offset = 0.0
                return True
        return False

    def handle_mouse(self, button: int, state: int, x: int, y: int) -> bool:
        return False

class AirplaneLayer(GuiLayer):
    '''Listen to user input setting the pointing offset.'''
    def __init__(self) -> None:
        # The airplanes to draw, as returned by sbs1_receiver.get_planes().
        self.iface_airplanes: dict[str, Airplane] = dict()
        self.airplanes: dict[str, Airplane] = dict()

        # Hex code of the airplane the user wants to track.
        self.iface_tracked_plane: str | None = None
        self.tracked_plane: str | None = None

    def update_planes(self, airplanes: dict[str, Airplane]) -> None:
        '''Update the locations of the planes in the GUI.'''
        self.gui.exit_if_dead()
        copied_airplanes = copy.deepcopy(airplanes)
        with self.gui.iface_lock:
            self.iface_airplanes = copied_airplanes

    def get_tracked_plane(self) -> str | None:
        '''Get the hex code of the airplane the user wants to track.'''
        self.gui.exit_if_dead()
        with self.gui.iface_lock:
            return self.iface_tracked_plane

    def stop_tracking(self) -> None:
        '''Call from the main thread to stop tracking the currently selected airplane.'''
        self.gui.exit_if_dead()
        with self.gui.iface_lock:
            self.iface_tracked_plane = None

    def pull_data_from_main_thread(self) -> None:
        self.airplanes = copy.deepcopy(self.iface_airplanes)
        self.tracked_plane = copy.deepcopy(self.iface_tracked_plane)

    def draw(self) -> None:
        '''Draw the airplanes.'''
        if self.gui.black_and_white:
            if self.gui.white_bg:
                all_black = (0.0, 0.0, 0.0)

                untracked_atmos_last_known_color = all_black
                untracked_space_last_known_color = all_black
                tracked_last_known_color         = all_black
                tracked_projected_color          = all_black
            else:
                all_white = (1.0, 1.0, 1.0)

                untracked_atmos_last_known_color = all_white
                untracked_space_last_known_color = all_white
                tracked_last_known_color         = all_white
                tracked_projected_color          = all_white
        else:
            if self.gui.white_bg:
                untracked_space_last_known_color = (0.0, 0.0, 0.0) # Black
            else:
                untracked_space_last_known_color = (0.8, 0.8, 0.8) # Gray
            untracked_atmos_last_known_color = (0.4, 0.4, 1.0) # Dark blue
            tracked_last_known_color         = (1.0, 0.4, 0.4) # Dark orange
            tracked_projected_color          = (1.0, 0.6, 0.6) # Light orange

        marker_radius = 0.5/180*math.pi

        for airplane in self.airplanes.values():
            # If this is the tracked airplane, draw it in one color scheme.
            # Draw untracked airplanes in a different color scheme.
            # Untracked "airplanes" in space get yet another color scheme.
            if self.tracked_plane == airplane.hex.value:
                last_known_color = tracked_last_known_color
            elif airplane.in_space:
                last_known_color = untracked_space_last_known_color
            else:
                last_known_color = untracked_atmos_last_known_color

            # Draw a marker for this airplane.
            self.gui.draw_marker(
                last_known_color,
                marker_radius,
                unwrap(airplane.az.value),
                unwrap(airplane.el.value),
                unwrap(airplane.callsign.value))

            # If this is the tracked airplane, extrapolate its current location based on the
            # last update and put a secondary marker there. Ideally we would do this for every airplane,
            # but the extrapolation is expensive and my laptop is only so fast. Maybe yours is faster.
            if self.tracked_plane == airplane.hex.value:
                extrapolated = airplane.extrapolate(time.monotonic_ns())
                self.gui.draw_marker(tracked_projected_color,
                                     marker_radius,
                                     unwrap(extrapolated.az.value),
                                     unwrap(extrapolated.el.value))

    def handle_key(self, key: bytes, x: int, y: int) -> bool:
        '''Stop tracking the airplane if the user presses escape.'''
        if key == b'\x1b':
            with self.gui.iface_lock:
                self.iface_tracked_plane = None
                return True
        return False

    def handle_mouse(self, button: int, state: int, x: int, y: int) -> bool:
        '''Clicking on an airplane will start tracking it.'''
        # Ignore anything but a left mouse button down event.
        if button != 0 or state != 1:
            return False

        # Determine which airplane is closest to the mouse click location
        # and begin tracking that airplane.
        min_dist = None
        closest = None
        for airplane in self.airplanes.values():
            ax, ay = self.gui.azm_alt_to_x_y(unwrap(airplane.az.value), unwrap(airplane.el.value))
            dist = math.sqrt((x-ax)**2 + (y-ay)**2)
            if (min_dist is None or dist < min_dist) and (dist < 50):
                min_dist = dist
                closest = airplane.hex.value

        with self.gui.iface_lock:
            self.iface_tracked_plane = closest

        return True

class TelescopeLayer(GuiLayer):
    '''Show the position of the telescope.'''
    def __init__(self) -> None:
        # Where is the telescope pointing? (azimuth, elevation). radians.
        self.iface_scope_azm_alt = (0.0, 0.0)
        self.scope_azm_alt = (0.0, 0.0)

    def update_telescope_location(self, scope_azm_alt: tuple[float, float]) -> None:
        '''Update the telescope location in the GUI.'''
        self.gui.exit_if_dead()
        with self.gui.iface_lock:
            self.iface_scope_azm_alt = scope_azm_alt

    def pull_data_from_main_thread(self) -> None:
        self.scope_azm_alt = self.iface_scope_azm_alt

    def draw(self) -> None:
        scope_azm, scope_alt = self.scope_azm_alt

        if self.gui.black_and_white:
            if self.gui.white_bg:
                telescope_color = (0.0, 0.0, 0.0)
            else:
                telescope_color = (1.0, 1.0, 1.0)
        else:
            telescope_color = (1.0, 0.0, 0.0)

        marker_radius = 0.5/180*math.pi

        # Draw a cross and circle where the telescope is pointing.
        self.gui.draw_marker(telescope_color, marker_radius, scope_azm, scope_alt)
        self.gui.draw_sky_circle(telescope_color, 1/180*math.pi, scope_azm, scope_alt)

        # Label the back of the telescope if NexPlane thinks its pointing at the ground,
        # because the user may be confused.
        if util.wrap_rad(scope_alt, -math.pi) < 0:
            back_azm = util.wrap_rad(scope_azm + math.pi, 0)
            back_alt = util.wrap_rad(-scope_alt, -math.pi)
            self.gui.draw_marker(telescope_color, marker_radius, back_azm, back_alt)
            x, y = self.gui.azm_alt_to_x_y(back_azm, back_alt)
            self.gui.draw_text(x+5, y+5, 'Back of telescope.')

    def handle_key(self, key: bytes, x: int, y: int) -> bool:
        return False

    def handle_mouse(self, button: int, state: int, x: int, y: int) -> bool:
        return False

class SunMoonLayer(GuiLayer):
    '''Show the position of the Sun and Moon.'''
    def __init__(self) -> None:
        # Where is the Sun? (azimuth, elevation). radians.
        self.iface_sun_azm_alt = (0.0, 0.0)
        self.sun_azm_alt = (0.0, 0.0)

        # Where is the Moon? (azimuth, elevation). radians.
        self.iface_moon_azm_alt = (0.0, 0.0)
        self.moon_azm_alt = (0.0, 0.0)

    def update_positions(
        self,
        sun_azm_alt: tuple[float, float],
        moon_azm_alt: tuple[float, float]) -> None:
        '''Update the Sun and Moon locations in the GUI.'''
        self.gui.exit_if_dead()
        with self.gui.iface_lock:
            self.iface_sun_azm_alt = sun_azm_alt
            self.iface_moon_azm_alt = moon_azm_alt

    def pull_data_from_main_thread(self) -> None:
        self.sun_azm_alt = self.iface_sun_azm_alt
        self.moon_azm_alt = self.iface_moon_azm_alt

    def draw(self) -> None:
        sun_azm, sun_alt = self.sun_azm_alt
        moon_azm, moon_alt = self.moon_azm_alt

        if self.gui.black_and_white:
            if self.gui.white_bg:
                all_black = (0.0, 0.0, 0.0)
                sun_color  = all_black
                moon_color = all_black
            else:
                all_white = (1.0, 1.0, 1.0)
                sun_color  = all_white
                moon_color = all_white
        else:
            if self.gui.white_bg:
                sun_color  = (1.0, 0.5, 0.0) # Dark yellow
                moon_color = (0.0, 0.0, 0.0) # Black
            else:
                sun_color  = (1.0, 1.0, 0.0) # Bright yellow
                moon_color = (0.8, 0.8, 0.8) # Gray

        # Draw the Moon.
        sun_moon_angular_radius = 0.26/180*math.pi
        self.gui.draw_marker(moon_color, sun_moon_angular_radius, moon_azm, moon_alt, 'Moon')
        self.gui.draw_sky_circle(moon_color, sun_moon_angular_radius, moon_azm, moon_alt)

        # Draw the Sun, and some bright warning circles around it.
        self.gui.draw_marker(sun_color, sun_moon_angular_radius, sun_azm, sun_alt, 'Sun')
        self.gui.draw_sky_circle(sun_color, sun_moon_angular_radius, sun_azm, sun_alt)
        self.gui.draw_sky_circle(sun_color, 5/180*math.pi, sun_azm, sun_alt)
        self.gui.draw_sky_circle(sun_color, 10/180*math.pi, sun_azm, sun_alt)
        self.gui.draw_sky_circle(sun_color, 15/180*math.pi, sun_azm, sun_alt)
        self.gui.draw_sky_circle(sun_color, 20/180*math.pi, sun_azm, sun_alt)

    def handle_key(self, key: bytes, x: int, y: int) -> bool:
        return False

    def handle_mouse(self, button: int, state: int, x: int, y: int) -> bool:
        return False

class EqFrameLayer(GuiLayer):
    '''Show the equatorial frame.'''
    def __init__(self, observatory_location: coords.EarthLocation) -> None:
        self.observatory_location = observatory_location

    def pull_data_from_main_thread(self) -> None:
        pass

    def draw(self) -> None:
        '''Draw the equatorial frame.'''
        if self.gui.black_and_white:
            if self.gui.white_bg:
                color        = (0.0, 0.0, 0.0)
                second_color = (0.7, 0.7, 0.7)
            else:
                color        = (1.0, 1.0, 1.0)
                second_color = (0.2, 0.2, 0.2)
        else:
            if self.gui.white_bg:
                color        = (0.9, 0.3, 1.0) # Purple
                second_color = (1.0, 0.8, 1.0) # Pink
            else:
                color        = (0.9, 0.3, 1.0) # Purple
                second_color = (0.2, 0.1, 0.2) # Dark Purple

        current_time = util.get_current_time()

        north_pole_alt, north_pole_azm = util.radec_to_altaz(0, math.pi/2, self.observatory_location, current_time)
        south_pole_alt, south_pole_azm = util.radec_to_altaz(0, -math.pi/2, self.observatory_location, current_time)

        pole_mark_radius = 3.0/180*math.pi

        # Draw the north pole
        self.gui.draw_sky_circle(color, pole_mark_radius, north_pole_azm, north_pole_alt)

        # Draw the south pole
        self.gui.draw_sky_circle(color, pole_mark_radius, south_pole_azm, south_pole_alt)

        # Draw the celestial plane
        self.gui.draw_sky_circle(color, math.pi/2, north_pole_azm, north_pole_alt)

        # Draw some dec lines
        for normal_ra in [0, math.pi/4, math.pi/2, -math.pi/4]:
            dec_line_normal_alt, dec_line_normal_azm = util.radec_to_altaz(normal_ra, 0, self.observatory_location, current_time)
            self.gui.draw_sky_circle(second_color, math.pi/2, dec_line_normal_azm, dec_line_normal_alt)

        # Draw some ra lines
        for dec_radius in [math.pi/8, math.pi/4, 3*math.pi/8, 5*math.pi/8, 3*math.pi/4, 7*math.pi/8]:
            self.gui.draw_sky_circle(second_color, dec_radius, north_pole_azm, north_pole_alt)

    def handle_key(self, key: bytes, x: int, y: int) -> bool:
        return False

    def handle_mouse(self, button: int, state: int, x: int, y: int) -> bool:
        return False

class HorizonLayer(GuiLayer):
    '''Show the horizon.'''
    def pull_data_from_main_thread(self) -> None:
        pass

    def draw(self) -> None:
        '''Draw the horizon and axis labels.'''
        if self.gui.black_and_white:
            if self.gui.white_bg:
                color = (0.0, 0.0, 0.0)
            else:
                color = (1.0, 1.0, 1.0)
        else:
            color = (0.2, 0.6, 0.2) # Green

        x1, y1 = self.gui.azm_alt_to_x_y(0, 0)
        x2, y2 = self.gui.azm_alt_to_x_y(1.999*math.pi, 0)

        gl.glBegin(gl.GL_LINES)
        gl.glColor3f(*color)
        gl.glVertex2f(x1, y1)
        gl.glVertex2f(x2, y2)
        gl.glEnd()

        compass_y = y2-15
        self.gui.draw_text(self.gui.width * 0.00 - 0, compass_y, 'N')
        self.gui.draw_text(self.gui.width * 0.25 - 5, compass_y, 'E')
        self.gui.draw_text(self.gui.width * 0.50 - 5, compass_y, 'S')
        self.gui.draw_text(self.gui.width * 0.75 - 5, compass_y, 'W')
        self.gui.draw_text(self.gui.width * 1.00 - 9, compass_y, 'N')

        for (alt, font_offset) in [(0,0), (30,7), (60,7), (90,10)]:
            x, y = self.gui.azm_alt_to_x_y(math.pi, alt/180*math.pi)
            self.gui.draw_text(x - 5, y - font_offset, str(alt))

    def handle_key(self, key: bytes, x: int, y: int) -> bool:
        return False

    def handle_mouse(self, button: int, state: int, x: int, y: int) -> bool:
        return False

class Gui:
    '''Runs the GUI and provides the interface between it and the main thread.'''
    def __init__(self, black_and_white: bool, white_bg: bool, layers: list[GuiLayer]):
        self.black_and_white = black_and_white
        self.white_bg = white_bg

        for layer in layers:
            layer.set_gui(self)
        self.layers = layers

        # Mutex to lock the interface variables of all the layers.
        self.iface_lock = threading.RLock()

        # Start the GUI thread.
        def run_thread() -> None:
            self._run_gui()
        self.stop_thread = False
        self.thread = threading.Thread(target=run_thread)
        self.thread.start()

    def close(self) -> None:
        '''Call from the main thread to close the GUI and join the GUI thread.'''
        self.stop_thread = True
        self.thread.join()

    def __del__(self) -> None:
        self.close()

    def exit_if_dead(self) -> None:
        '''If the GUI thread is dead, raise Exit in the main thread.'''
        if not self.thread.is_alive():
            raise Exit

    def _run_gui(self) -> None:
        '''The GUI thread.'''
        # These variables track the current window size. Here are some reasonable defaults.
        self.width = 1200
        self.height = 900

        # Some boring OpenGL setup stuff.
        glut.glutInit([sys.argv[0]])
        glut.glutInitDisplayMode(glut.GLUT_RGBA | glut.GLUT_DOUBLE | glut.GLUT_DEPTH)
        glut.glutInitWindowSize(self.width, self.height)
        glut.glutInitWindowPosition(0, 0)
        self.window = glut.glutCreateWindow("NexPlane")
        glut.glutSetOption(glut.GLUT_ACTION_ON_WINDOW_CLOSE, glut.GLUT_ACTION_GLUTMAINLOOP_RETURNS)

        # Bind the important GLUT callbacks to member functions of this object.
        glut.glutDisplayFunc(self._draw)
        glut.glutReshapeFunc(self._handle_resize)
        glut.glutKeyboardFunc(self._handle_key)
        glut.glutMouseFunc(self._handle_mouse)
        glut.glutCloseFunc(self._handle_close_window)

        # More boring OpenGL setup stuff. Set the background color depending on flag.
        if self.white_bg:
            gl.glClearColor(255.0, 255.0, 255.0, 0.0)
        else:
            gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClearDepth(1.0)
        gl.glDepthFunc(gl.GL_LEQUAL)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glShadeModel(gl.GL_SMOOTH)

        # We're going to set up OpenGL for an orthographic projection.
        # This will make it easy to draw the sky with an equirectangular projection.
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()
        gl.glOrtho(0, self.width, 0, self.height, 0.0, 1.0)
        gl.glMatrixMode(gl.GL_MODELVIEW)

        # GUI loop. Runs at just under 10Hz.
        while not self.stop_thread:
            glut.glutPostRedisplay()
            glut.glutMainLoopEvent()
            time.sleep(0.1)

    def _handle_close_window(self) -> None:
        '''Callback for the window being closed. Signals the GUI loop, and therefore the GUI thread, to exit.'''
        self.stop_thread = True

    def _handle_resize(self, width: int, height: int) -> None:
        '''Callback to handle a resize of the window.'''
        self.width = max(640, width)
        self.height = max(480, height)

        gl.glViewport(0, 0, self.width, self.height)
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()
        gl.glOrtho(0, self.width, 0, self.height, 0.0, 1.0)
        gl.glMatrixMode(gl.GL_MODELVIEW)

    def _handle_key(self, key: bytes, x: int, y: int) -> None:
        '''Callback to handle a keypress.'''
        for layer in reversed(self.layers):
            if layer.handle_key(key, x, y):
                break

    def _handle_mouse(self, button: int, state: int, x: int, y: int) -> None:
        '''Callback to handle a mouse event.'''
        y = self.height - y

        for layer in reversed(self.layers):
            if layer.handle_mouse(button, state, x, y):
                break

    def azm_alt_to_x_y(self, azm: float, alt: float) -> tuple[float, float]:
        '''Compute window coordinates corresponding to a particular azimuth and elevation.'''
        min_alt = -5 / 180 * math.pi
        max_alt = math.pi / 2

        x = int(azm/(2*math.pi)*self.width)
        y = int(((alt-min_alt)/(max_alt-min_alt)) * self.height)
        return x, y

    def _draw(self) -> None:
        '''Callback for drawing the window contents.'''
        # Get new data from the main thread.
        with self.iface_lock:
            for layer in self.layers:
                layer.pull_data_from_main_thread()

        # Clear the window and prepare for drawing.
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glLoadIdentity()

        # Draw the layers.
        for layer in self.layers:
            layer.draw()

        # Update the screen.
        glut.glutSwapBuffers()

    def draw_text(self, x: float, y: float, text: str, font: int = glut.GLUT_BITMAP_9_BY_15) -> None:
        '''Draw some text at the given location on screen.'''
        gl.glRasterPos2f(x, y)
        glut.glutBitmapString(font, text.encode())

    def draw_marker(self, color: Color, radius: float, azm: float, alt: float, label: str = '') -> None:
        '''Draw a marker on the screen.

        color    The color of the marker.
        radius   The radius of the marker, in radians.
        azm      The azimuth of the marker, in radians.
        alt      The elevation of the marker, in radians.
        label    An optional text label to write beside the marker.
        '''
        x,  y  = self.azm_alt_to_x_y(azm, alt)
        xp, yp = self.azm_alt_to_x_y(azm+radius, alt+radius)
        xm, ym = self.azm_alt_to_x_y(azm-radius, alt-radius)

        gl.glBegin(gl.GL_LINES)
        gl.glColor3f(*color)
        gl.glVertex2f(x, yp)
        gl.glVertex2f(x, ym)
        gl.glVertex2f(xp, y)
        gl.glVertex2f(xm, y)
        gl.glEnd()

        if label != '':
            self.draw_text(x+4, y-4, label)

    def draw_sky_circle(self, color: Color, angular_radius: float, center_azm: float, center_alt: float) -> None:
        '''Draw a circle on the sky, and project it onto the screen using the equiangular projection.

        color           The color of the circle
        angular_radius  The radius of the circle, in radians.
        center_azm      The azimuth of the center of the circle.
        center_alt      The elevation of the center of the circle.
        '''
        # Unit vector towards the center, in North East Down coordinates.
        center_ned = util.aer_to_ned(center_azm, center_alt, 1.0)

        # Use the cross product to find another vector perpendicular to center_ned.
        cross_options = [[1,0,0], [0,1,0]]
        if numpy.dot(cross_options[0], center_ned) > numpy.dot(cross_options[1], center_ned):
            cross = numpy.cross(cross_options[1], center_ned)
        else:
            cross = numpy.cross(cross_options[0], center_ned)

        # Rotate center_ned around the perpendicular vector to find a vector that points at some location on the edge of the circle.
        scribe = scipy.spatial.transform.Rotation.from_rotvec(util.normalize(cross) * angular_radius).apply(center_ned)

        # Rotate the scribe vector in a full circle around center_ned, drawing a line strip in the requested color.
        gl.glBegin(gl.GL_LINE_STRIP)
        gl.glColor3f(*color)
        npoints = 15 + 2 * int(angular_radius/math.pi*180)
        x_prev = None
        y_prev = None
        for i in range(npoints+1):
            angle = i * 2*math.pi/npoints
            azm, alt, _ = util.ned_to_aer(scipy.spatial.transform.Rotation.from_rotvec(center_ned*angle).apply(scribe))
            x, y = self.azm_alt_to_x_y(azm, alt)

            # If the circle wraps across the screen from side to side then restart the line strip at the edges
            # of the screen to avoid visual weirdness.
            if x_prev is not None and abs(x - x_prev) > self.width/2:
                assert y_prev is not None
                # y_mid is just an approximation, but it's good enough.
                y_mid = (y+y_prev)/2
                if x_prev > self.width/2:
                    gl.glVertex2f(self.width, y_mid)
                    gl.glEnd()
                    gl.glBegin(gl.GL_LINE_STRIP)
                    gl.glVertex2f(0, y_mid)
                else:
                    gl.glVertex2f(0, y_mid)
                    gl.glEnd()
                    gl.glBegin(gl.GL_LINE_STRIP)
                    gl.glVertex2f(self.width, y_mid)

            gl.glVertex2f(x, y)

            x_prev = x
            y_prev = y

        gl.glEnd()
