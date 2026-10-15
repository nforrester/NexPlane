#!/usr/bin/env python3
'''
Checks whether the built-in LTE modem (e.g. Dell DW5821e) has a GPS receiver and
delivers a position.

    ./gps_check.py                 (wait up to 3 minutes for a position)
    ./gps_check.py --duration 600  (wait longer, e.g. the first time / cold start)

Best run outdoors or near a window: the modem uses the laptop antennas,
a cold start can take several minutes.

The script
  1. shows whether mobile/WWAN is disabled via rfkill (airplane mode),
  2. asks ModemManager (mmcli) for the modem's location capabilities,
  3. enables GPS there and waits for a position,
  4. additionally listens passively on the modem ports for NMEA data ($GPGGA ...).
The mount (Prolific/CH340/FTDI port) is not touched.
'''

import argparse
import glob
import os
import re
import select
import shutil
import subprocess
import time

MODEM_DRIVERS = ('option', 'option1', 'qcserial', 'sierra')

def run(cmd: list[str], timeout: float = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f'{cmd[0]} is not installed'
    except subprocess.TimeoutExpired:
        return 124, 'timeout'

def driver(tty: str) -> str:
    try:
        return os.path.basename(os.path.realpath(f'/sys/class/tty/{os.path.basename(tty)}/device/driver'))
    except OSError:
        return ''

def parse_nmea_gga(line: str) -> tuple[float, float, float, int, int] | None:
    '''$..GGA -> (latitude, longitude, altitude, fix quality, satellites) or None.'''
    f = line.split(',')
    if len(f) < 10 or not f[0].endswith('GGA') or not f[2] or not f[4]:
        return None
    try:
        lat = int(f[2][:2]) + float(f[2][2:]) / 60
        lon = int(f[4][:3]) + float(f[4][3:]) / 60
        if f[3] == 'S':
            lat = -lat
        if f[5] == 'W':
            lon = -lon
        return lat, lon, float(f[9] or 0), int(f[6] or 0), int(f[7] or 0)
    except ValueError:
        return None

def inject_xtra(modem: str, location_status: str) -> None:
    import urllib.request
    urls = re.findall(r'https?://\S+?\.bin', location_status) or ['https://path1.xtracloud.net/xtra3grcej.bin']
    path = '/tmp/nexplane_xtra.bin'
    for url in urls:
        try:
            print(f'  Downloading assistance data from {url} ...')
            with urllib.request.urlopen(url, timeout=20) as r, open(path, 'wb') as f:
                f.write(r.read())
            break
        except Exception as e:
            print(f'    failed: {e}')
    else:
        print('  The assistance data could not be downloaded (internet?).')
        return
    code, out = run(['mmcli', '-m', modem, f'--location-inject-assistance-data={path}'], timeout=60)
    print('  Assistance data to the modem: ' + (out or 'ok'))
    if code != 0:
        print('  -> That did not work. Try again with sudo.')

def gsv_summary(sats: dict[str, int]) -> str:
    good = sorted((snr for snr in sats.values() if snr > 0), reverse=True)
    return f'{len(sats)} satellites seen, {len(good)} with signal' + (f' (best {good[:4]} dB)' if good else '')

def main() -> None:
    parser = argparse.ArgumentParser(description='Checks the GPS receiver in the LTE modem.')
    parser.add_argument('--duration', type=int, default=180, help='Seconds to wait for a position (default 180)')
    parser.add_argument('--xtra', action='store_true',
                        help='Download assistance data (satellite orbits) over the laptop\'s internet connection and '
                             'pass it to the modem; shortens a cold start a lot. Usually needs sudo.')
    args = parser.parse_args()

    print('== 1. Flight mode / rfkill')
    code, out = run(['rfkill', 'list'])
    print(out if code == 0 else f'  rfkill: {out}')
    if re.search(r'(?is)wwan.*?Soft blocked: yes', out):
        print('  -> Mobile broadband (WWAN) is disabled. Enable it with: rfkill unblock wwan')

    print('\n== 2. Modem ports')
    ports = [p for p in sorted(glob.glob('/dev/ttyUSB*')) + sorted(glob.glob('/dev/wwan*')) if driver(p) in MODEM_DRIVERS or '/wwan' in p]
    for p in ports:
        print(f'  {p}  (driver {driver(p) or "?"})')
    if not ports:
        print('  no modem ports found')

    print('\n== 3. ModemManager')
    modem = None
    if not shutil.which('mmcli'):
        print('  mmcli is not installed (package modemmanager). Skipping.')
    else:
        code, out = run(['mmcli', '-L'])
        print('  ' + out.replace('\n', '\n  '))
        m = re.search(r'/Modem/(\d+)', out)
        if m:
            modem = m.group(1)
            code, out = run(['mmcli', '-m', modem])
            for line in out.splitlines():
                if any(k in line for k in ('model', 'manufacturer', 'firmware', 'state', 'power state')):
                    print('  ' + line.strip())
            code, out = run(['mmcli', '-m', modem, '--location-status'])
            print('  Location:\n  ' + out.replace('\n', '\n  '))
            if 'gps' not in out.lower():
                print('  -> ModemManager reports no GPS capability for this modem.')
            if args.xtra:
                inject_xtra(modem, out)
            print('  Enabling GPS ...')
            code, out = run(['mmcli', '-m', modem, '--location-enable-gps-nmea', '--location-enable-gps-raw'])
            print('  ' + (out or 'ok'))
            if code != 0 and ('not authorized' in out.lower() or 'permission' in out.lower()):
                print('  -> Permission denied. Please run the script again with sudo.')
        else:
            print('  No modem registered with ModemManager (flight mode? ModemManager not running?).')

    print(f'\n== 4. Waiting up to {args.duration} s for a position (Ctrl+C aborts)')
    # Listen passively on the modem ports for NMEA (read only, send no commands)
    fds: dict[int, str] = {}
    for p in ports:
        if not p.startswith('/dev/ttyUSB'):
            continue
        try:
            fds[os.open(p, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)] = p
        except OSError as e:
            print(f'  {p} is not readable ({e.strerror})')
    buffers = {fd: '' for fd in fds}
    nmea_seen: set[str] = set()
    sats: dict[str, int] = {}
    last_report = 0.0
    t0 = time.monotonic()
    last_mm = 0.0
    found = None
    try:
        while time.monotonic() - t0 < args.duration and found is None:
            if fds:
                ready, _, _ = select.select(list(fds), [], [], 1.0)
                for fd in ready:
                    try:
                        buffers[fd] += os.read(fd, 4096).decode(errors='ignore')
                    except OSError:
                        continue
                    *lines, buffers[fd] = buffers[fd].split('\n')
                    for line in lines:
                        line = line.strip()
                        if line.startswith('$'):
                            if fds[fd] not in nmea_seen:
                                print(f'  NMEA data on {fds[fd]}: {line[:60]}')
                                nmea_seen.add(fds[fd])
                            if 'GSV' in line[:7]:
                                f = line.split('*')[0].split(',')
                                for k in range(4, len(f) - 3, 4):
                                    if f[k]:
                                        sats[line[1:3] + f[k]] = int(f[k + 3]) if f[k + 3].isdigit() else 0
                            g = parse_nmea_gga(line)
                            if g and g[3] > 0:
                                found = (g, f'NMEA on {fds[fd]}')
            else:
                time.sleep(1.0)
            if modem and time.monotonic() - last_mm > 5:
                last_mm = time.monotonic()
                code, out = run(['mmcli', '-m', modem, '--location-get'])
                for line in out.splitlines():
                    if 'GGA' in line:
                        g = parse_nmea_gga(line[line.index('$'):].strip()) if '$' in line else None
                        if g and g[3] > 0:
                            found = (g, 'ModemManager (NMEA)')
                m_lat = re.search(r'latitude:\s*([-\d.]+)', out)
                m_lon = re.search(r'longitude:\s*([-\d.]+)', out)
                if m_lat and m_lon and found is None and float(m_lat.group(1)) != 0:
                    m_alt = re.search(r'altitude:\s*([-\d.]+)', out)
                    found = ((float(m_lat.group(1)), float(m_lon.group(1)),
                              float(m_alt.group(1)) if m_alt else 0.0, 1, 0), 'ModemManager')
            if time.monotonic() - last_report > 30:
                last_report = time.monotonic()
                print(f'  {int(time.monotonic() - t0):4d} s: no position yet; {gsv_summary(sats)}', flush=True)
    except KeyboardInterrupt:
        pass
    for fd in fds:
        os.close(fd)

    print('\n== Result')
    if found:
        (lat, lon, alt, fix, n_sats), src = found
        print(f'  GPS WORKS ({src}): latitude {lat:.6f}, longitude {lon:.6f}, altitude {alt:.0f} m'
              + (f', {n_sats} satellites' if n_sats else ''))
    elif nmea_seen:
        print('  GPS receiver present (NMEA data arrives), but no position yet. '
              'Try longer and under the open sky: --duration 600')
    elif modem:
        print('  No position and no NMEA data. Possible causes: the modem has no GPS, GPS is disabled in the '
              'firmware, or it needs longer (--duration 600, outdoors).')
    else:
        print('  No modem reachable. Check rfkill/flight mode and ModemManager.')
    print('  Please send back the complete output.')

if __name__ == '__main__':
    main()
