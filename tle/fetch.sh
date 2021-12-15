#!/bin/bash

# Fetch some interesting TLE datasets from CelesTrak.

set -ex

rm -f visual.txt
rm -f starlink.txt
rm -f geo.txt
rm -f gps-ops.txt
rm -f stations.txt

wget https://celestrak.com/NORAD/elements/visual.txt
wget https://celestrak.com/NORAD/elements/starlink.txt
wget https://celestrak.com/NORAD/elements/geo.txt
wget https://celestrak.com/NORAD/elements/gps-ops.txt
wget https://celestrak.com/NORAD/elements/stations.txt
