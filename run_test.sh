#!/bin/bash

# This script helps run a library of useful testcases to exercise NexPlane in HOOTL.

if [ $# -eq 0 ]; then
    set -ex
    ./run_test.sh 0
    ./run_test.sh 1
    ./run_test.sh 2
    ./run_test.sh 3
    ./run_test.sh 4
    ./run_test.sh 5
    ./run_test.sh 6
    ./run_test.sh 7
    ./run_test.sh 8
    ./run_test.sh 9
    ./run_test.sh 10
    ./run_test.sh 11
    ./run_test.sh 12
    exit 0
fi

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
    SCOPE_PORT=45345
    ./telescope_server.py --hootl --location $LOCATION --network-port $SCOPE_PORT --telescope-protocol nexstar-hand-control --mount-mode altaz &
    SCOPE_PROC=$!
    ./nexplane.py --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol nexstar-hand-control --mount-mode altaz --telescope localhost:$SCOPE_PORT
    kill $SCOPE_PROC

elif [ $CASE -eq 10 ]; then
    SCOPE_PORT=45345
    ./telescope_server.py --hootl --location $LOCATION --network-port $SCOPE_PORT --telescope-protocol skywatcher-mount-head-usb --mount-mode altaz &
    SCOPE_PROC=$!
    ./nexplane.py --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol skywatcher-mount-head-usb --mount-mode altaz --telescope localhost:$SCOPE_PORT
    kill $SCOPE_PROC

elif [ $CASE -eq 11 ]; then
    SCOPE_PORT=45345
    ./telescope_server.py --hootl --location $LOCATION --network-port $SCOPE_PORT --telescope-protocol skywatcher-mount-head-eqmod --mount-mode altaz &
    SCOPE_PROC=$!
    ./nexplane.py --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol skywatcher-mount-head-eqmod --mount-mode altaz --telescope localhost:$SCOPE_PORT
    kill $SCOPE_PROC

elif [ $CASE -eq 12 ]; then
    SCOPE_PORT=11880
    ./skywatcher_wifi_hootl.py $SCOPE_PORT &
    SCOPE_PROC=$!
    ./nexplane.py --location $LOCATION --landmark $LANDMARK --sbs1 localhost:$SAT_PORT --telescope-protocol skywatcher-mount-head-wifi --mount-mode altaz --telescope localhost:$SCOPE_PORT
    kill $SCOPE_PROC

else
    echo "No such test case: " $CASE
fi

kill $SAT_PROC
