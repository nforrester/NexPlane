'''Graphical User Interface. Uses OpenGL/GLUT. Draws the sky using an equiangular projection.

The GUI runs in a separate thread, but the functions you use to feed it data or see what the
user wants can be called from the main thread. They just lock a mutex internally.
'''

import copy
import math
import numpy
import scipy.spatial
import sys
import threading
import time

import OpenGL.GL as gl
import OpenGL.GLU as glu
import OpenGL.GLUT as glut

import util

class Exit(Exception):
    '''Thrown in the main thread when the GUI thread stops, probably because somebody closed the window.'''
    pass

class Gui(object):
    '''Runs the GUI and provides the interface between it and the main thread.'''
    def __init__(self, black_and_white, kp, ki, kd):
        self.black_and_white = black_and_white

        # Interface variables, shared between main and gui thread.
        self.iface_scope_azm_alt = (0.0, 0.0)  # Where is the telescope pointing? (azimuth, elevation). radians.
        self.iface_sun_azm_alt = (0.0, 0.0)    # Where is the Sun?                (azimuth, elevation). radians.
        self.iface_moon_azm_alt = (0.0, 0.0)   # Where is the Moon?               (azimuth, elevation). radians.
        self.iface_airplanes = dict()          # The airplanes to draw, as returned by sbs1_receiver.get_planes().
        self.iface_tracked_plane = None        # Hex code of the airplane the user wants to track.

        # Manually applied azimuth and elevation offsets.
        self.iface_azm_offset = 0.0
        self.iface_alt_offset = 0.0

        self.iface_kp = kp           # Proportional gain for the Tracker's PidControllers. Can be adjusted by the user.
        self.iface_ki = ki           # Integral     gain for the Tracker's PidControllers. Can be adjusted by the user.
        self.iface_kd = kd           # Derivative   gain for the Tracker's PidControllers. Can be adjusted by the user.
        self.iface_gain_changes = 0  # How many times has the user changed the gains? Useful for knowing when to reset the PidControllers.

        # Mutex to lock the self.iface_* variables.
        self.iface_lock = threading.Lock()

        # Start the GUI thread.
        def run_thread():
            self._run_gui()
        self.stop_thread = False
        self.thread = threading.Thread(target=run_thread)
        self.thread.start()

    def close(self):
        '''Call from the main thread to close the GUI and join the GUI thread.'''
        self.stop_thread = True
        self.thread.join()

    def __del__(self):
        self.close()

    def provide_update(self, scope_azm_alt, sun_azm_alt, moon_azm_alt, airplanes):
        '''Call from the main thread to feed new data to the GUI so it can be displayed. See comments in __init__() for parameter definitions.'''
        if not self.thread.is_alive():
            raise Exit
        copied_airplanes = copy.deepcopy(airplanes)
        with self.iface_lock:
            self.iface_scope_azm_alt = scope_azm_alt
            self.iface_sun_azm_alt = sun_azm_alt
            self.iface_moon_azm_alt = moon_azm_alt
            self.iface_airplanes = copied_airplanes

    def get_inputs(self):
        '''Call from the main thread to get the user's latest desires. See comments in __init__() for return value definitions.'''
        with self.iface_lock:
            return self.iface_tracked_plane, self.iface_azm_offset, self.iface_alt_offset, self.iface_kp, self.iface_ki, self.iface_kd, self.iface_gain_changes

    def stop_tracking(self):
        '''Call from the main thread to stop tracking the currently selected airplane.'''
        with self.iface_lock:
            self.iface_tracked_plane = None
            self.iface_azm_offset = 0.0
            self.iface_alt_offset = 0.0

    def _run_gui(self):
        '''The GUI thread.'''
        # These variables track the current window size. Here are some reasonable defaults.
        self.width = 1200
        self.height = 900

        # Some boring OpenGL setup stuff.
        glut.glutInit([sys.argv[0]])
        glut.glutInitDisplayMode(glut.GLUT_RGBA | glut.GLUT_DOUBLE | glut.GLUT_DEPTH)
        glut.glutInitWindowSize(self.width, self.height)
        glut.glutInitWindowPosition(0, 0)
        self.window = glut.glutCreateWindow("NexStar")
        glut.glutSetOption(glut.GLUT_ACTION_ON_WINDOW_CLOSE, glut.GLUT_ACTION_GLUTMAINLOOP_RETURNS)

        # Bind the important GLUT callbacks to member functions of this object.
        glut.glutDisplayFunc(self._draw)
        glut.glutReshapeFunc(self._handle_resize)
        glut.glutKeyboardFunc(self._handle_key)
        glut.glutMouseFunc(self._handle_mouse)
        glut.glutCloseFunc(self._handle_close_window)

        # More boring OpenGL setup stuff. Set the background to black.
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

    def _handle_close_window(self):
        '''Callback for the window being closed. Signals the GUI loop, and therefore the GUI thread, to exit.'''
        self.stop_thread = True

    def _azm_alt_to_x_y(self, azm, alt):
        '''Compute window coordinates corresponding to a particular azimuth and elevation.'''
        min_alt = -5 / 180 * math.pi
        max_alt = math.pi / 2

        x = int(azm/(2*math.pi)*self.width)
        y = int(((alt-min_alt)/(max_alt-min_alt)) * self.height)
        return x, y

    def _draw(self):
        '''Callback for drawing the window contents.'''
        # Get new data from the main thread.
        with self.iface_lock:
            scope_azm, scope_alt = self.iface_scope_azm_alt
            sun_azm, sun_alt = self.iface_sun_azm_alt
            moon_azm, moon_alt = self.iface_moon_azm_alt
            airplanes = copy.deepcopy(self.iface_airplanes)

        # Clear the window and prepare for drawing.
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glLoadIdentity()

        if self.black_and_white:
            # Forget the colors and make them all white (the background is black).
            # This is useful for raising contrast when operating in direct sunlight.
            yellow  = (1.0, 1.0, 1.0)
            red     = (1.0, 1.0, 1.0)
            blue    = (1.0, 1.0, 1.0)
            blue2   = (1.0, 1.0, 1.0)
            green   = (1.0, 1.0, 1.0)
            orange  = (1.0, 1.0, 1.0)
            orange2 = (1.0, 1.0, 1.0)
            gray    = (1.0, 1.0, 1.0)
        else:
            # Define some useful colors
            yellow  = (1.0, 1.0, 0.0)
            red     = (1.0, 0.0, 0.0)
            blue    = (0.4, 0.4, 1.0)
            blue2   = (0.6, 0.6, 1.0)
            green   = (0.2, 0.6, 0.2)
            orange  = (1.0, 0.4, 0.4)
            orange2 = (1.0, 0.6, 0.6)
            gray    = (0.8, 0.8, 0.8)

        # Draw the horizon and axis labels in green.
        self._draw_horizon(green)

        # Radius of the telescope field of view, in radians.
        # The present value is a bit high for my scope,
        # but it just controls the size of some markers on the screen so it's not very important.
        view_radius = 0.5/180*math.pi

        # Draw a red cross and circle where the telescope is pointing.
        self._draw_marker(red, view_radius, scope_azm, scope_alt)
        self._draw_sky_circle(red, 1/180*math.pi, scope_azm, scope_alt)

        # Draw the Moon in gray.
        sun_moon_angular_radius = 0.26/180*math.pi
        self._draw_marker(gray, sun_moon_angular_radius, moon_azm, moon_alt, 'Moon')
        self._draw_sky_circle(gray, sun_moon_angular_radius, moon_azm, moon_alt)

        # Draw the Sun in yellow, and some bright warning circles around it.
        self._draw_marker(yellow, sun_moon_angular_radius, sun_azm, sun_alt, 'Sun')
        self._draw_sky_circle(yellow, sun_moon_angular_radius, sun_azm, sun_alt)
        self._draw_sky_circle(yellow, 5/180*math.pi, sun_azm, sun_alt)
        self._draw_sky_circle(yellow, 10/180*math.pi, sun_azm, sun_alt)
        self._draw_sky_circle(yellow, 15/180*math.pi, sun_azm, sun_alt)
        self._draw_sky_circle(yellow, 20/180*math.pi, sun_azm, sun_alt)

        # Draw the airplanes.
        for airplane in airplanes.values():
            # If this is the tracked airplane, draw it in orange. Draw untracked airplanes in blue.
            with self.iface_lock:
                if self.iface_tracked_plane == airplane.hex.value:
                    color = orange
                    color2 = orange2
                else:
                    color = blue
                    color2 = blue2

            # Draw a marker for this airplane.
            self._draw_marker(color, view_radius, airplane.az.value, airplane.el.value, airplane.callsign.value)

            # If this is the tracked airplane, extrapolate its current location based on the
            # last update and put a secondary marker there. Ideally we would do this for every airplane,
            # but the extrapolation is expensive and my laptop is only so fast. Maybe yours is faster.
            if self.iface_tracked_plane == airplane.hex.value:
                extrapolated = airplane.extrapolate(time.monotonic_ns())
                self._draw_marker(color2, view_radius, extrapolated.az.value, extrapolated.el.value)

        # Update the screen.
        glut.glutSwapBuffers()

    def _draw_text(self, x, y, text):
        '''Draw some text at the given location on screen.'''
        gl.glRasterPos2f(x, y)
        glut.glutBitmapString(glut.GLUT_BITMAP_9_BY_15, text.encode())

    def _draw_horizon(self, color):
        '''Draw the horizon and axis labels.'''
        x1, y1 = self._azm_alt_to_x_y(0, 0)
        x2, y2 = self._azm_alt_to_x_y(1.999*math.pi, 0)

        gl.glBegin(gl.GL_LINES)
        gl.glColor3f(*color)
        gl.glVertex2f(x1, y1)
        gl.glVertex2f(x2, y2)
        gl.glEnd()

        compass_y = y2-15
        self._draw_text(self.width * 0.00 - 0, compass_y, 'N')
        self._draw_text(self.width * 0.25 - 5, compass_y, 'E')
        self._draw_text(self.width * 0.50 - 5, compass_y, 'S')
        self._draw_text(self.width * 0.75 - 5, compass_y, 'W')
        self._draw_text(self.width * 1.00 - 9, compass_y, 'N')

        for (alt, font_offset) in [(0,0), (30,7), (60,7), (90,10)]:
            x, y = self._azm_alt_to_x_y(math.pi, alt/180*math.pi)
            self._draw_text(x - 5, y - font_offset, str(alt))

    def _draw_marker(self, color, radius, azm, alt, label=''):
        '''Draw a marker on the screen.

        color    The color of the marker.
        radius   The radius of the marker, in radians.
        azm      The azimuth of the marker, in radians.
        alt      The elevation of the marker, in radians.
        label    An optional text label to write beside the marker.
        '''
        x,  y  = self._azm_alt_to_x_y(azm, alt)
        xp, yp = self._azm_alt_to_x_y(azm+radius, alt+radius)
        xm, ym = self._azm_alt_to_x_y(azm-radius, alt-radius)

        gl.glBegin(gl.GL_LINES)
        gl.glColor3f(*color)
        gl.glVertex2f(x, yp)
        gl.glVertex2f(x, ym)
        gl.glVertex2f(xp, y)
        gl.glVertex2f(xm, y)
        gl.glEnd()

        if label != '':
            self._draw_text(x+4, y-4, label)

    def _draw_sky_circle(self, color, angular_radius, center_azm, center_alt):
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
            x, y = self._azm_alt_to_x_y(azm, alt)

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

    def _handle_resize(self, width, height):
        '''Callback to handle a resize of the window.'''
        self.width = max(640, width)
        self.height = max(480, height)

        gl.glViewport(0, 0, self.width, self.height)
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()
        gl.glOrtho(0, self.width, 0, self.height, 0.0, 1.0)
        gl.glMatrixMode(gl.GL_MODELVIEW)

    def _handle_key(self, key, x, y):
        '''Callback to handle a keypress.'''
        small_motion = 0.1 / 180 * math.pi
        big_motion = 5 * small_motion

        with self.iface_lock:
            # Escape will stop tracking the current airplane.
            if key == b'\x1b':
                self.iface_tracked_plane = None
            # WASD or HJKL add manual offsets to the target location.
            # Capital letters make larger movements.
            elif key == b'w' or key == b'k':
                self.iface_alt_offset += small_motion
            elif key == b'a' or key == b'h':
                self.iface_azm_offset -= small_motion
            elif key == b's' or key == b'j':
                self.iface_alt_offset -= small_motion
            elif key == b'd' or key == b'l':
                self.iface_azm_offset += small_motion
            elif key == b'W' or key == b'K':
                self.iface_alt_offset += big_motion
            elif key == b'A' or key == b'H':
                self.iface_azm_offset -= big_motion
            elif key == b'S' or key == b'J':
                self.iface_alt_offset -= big_motion
            elif key == b'D' or key == b'L':
                self.iface_azm_offset += big_motion
            # Q or O resets the offset.
            elif key in [b'q', b'Q', b'o', b'O']:
                self.iface_azm_offset = 0.0
                self.iface_alt_offset = 0.0
            # RFTGYH adjust the PID controller gains up and down.
            elif key in [b'r', b'R']:
                self.iface_kp += 0.01
                self.iface_gain_changes += 1
            elif key in [b'f', b'F']:
                self.iface_kp -= 0.01
                self.iface_gain_changes += 1
            elif key in [b't', b'T']:
                self.iface_ki += 0.01
                self.iface_gain_changes += 1
            elif key in [b'g', b'G']:
                self.iface_ki -= 0.01
                self.iface_gain_changes += 1
            elif key in [b'y', b'Y']:
                self.iface_kd += 0.01
                self.iface_gain_changes += 1
            elif key in [b'h', b'H']:
                self.iface_kd -= 0.01
                self.iface_gain_changes += 1

    def _handle_mouse(self, button, state, x, y):
        '''Callback to handle a mouse event. Clicking on an airplane will start tracking it.'''
        # Ignore anything but a left mouse button down event.
        if button != 0 or state != 1:
            return

        y = self.height - y

        # Determine which airplane is closest to the mouse click location
        # and begin tracking that airplane.
        with self.iface_lock:
            airplanes = copy.deepcopy(self.iface_airplanes)

        min_dist = None
        closest = None
        for airplane in airplanes.values():
            ax, ay = self._azm_alt_to_x_y(airplane.az.value, airplane.el.value)
            dist = math.sqrt((x-ax)**2 + (y-ay)**2)
            if (closest is None or dist < min_dist) and (dist < 50):
                min_dist = dist
                closest = airplane.hex.value

        with self.iface_lock:
            self.iface_tracked_plane = closest
