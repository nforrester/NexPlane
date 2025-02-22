#!/bin/bash

# This script helps run a library of useful testcases to exercise NexPlane in HOOTL.

CASE=$1
LOCATION=griffith
LANDMARK=hollywood_sign
SAT_PORT=40004

set -x
./satellites.py tle/geo.txt --location $LOCATION --port $SAT_PORT &
SAT_PROC=$!

if [ $CASE -eq 0 ]; then
    ./nexplane.py --hootl --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol nexstar-hand-control --mount-mode altaz
elif [ $CASE -eq 1 ]; then
    ./nexplane.py --hootl --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol nexstar-hand-control --mount-mode eq
elif [ $CASE -eq 2 ]; then
    ./nexplane.py --hootl --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol nexstar-hand-control --mount-mode altaz --bw
elif [ $CASE -eq 3 ]; then
    ./nexplane.py --hootl --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol nexstar-hand-control --mount-mode altaz --bw --white-bg
elif [ $CASE -eq 4 ]; then
    ./nexplane.py --hootl --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol nexstar-hand-control --mount-mode altaz --white-bg
elif [ $CASE -eq 5 ]; then
    ./nexplane.py --hootl --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol skywatcher-mount-head-usb --mount-mode altaz
elif [ $CASE -eq 6 ]; then
    ./nexplane.py --hootl --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol skywatcher-mount-head-usb --mount-mode eq
elif [ $CASE -eq 7 ]; then
    ./nexplane.py --hootl --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol skywatcher-mount-head-eqmod --mount-mode altaz
elif [ $CASE -eq 8 ]; then
    ./nexplane.py --hootl --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol skywatcher-mount-head-wifi --mount-mode altaz
elif [ $CASE -eq 9 ]; then
    SCOPE_PORT=11880
    ./skywatcher_wifi_hootl.py $SCOPE_PORT &
    SCOPE_PROC=$!
    ./nexplane.py --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol skywatcher-mount-head-wifi --mount-mode altaz --telescope localhost:$SCOPE_PORT
    kill $SCOPE_PROC
else
    echo "No such test case: " $CASE
fi

kill $SAT_PROC
