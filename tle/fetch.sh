#!/bin/bash

# Fetch some interesting TLE datasets from CelesTrak.

set -ex

rm -f visual.txt
rm -f starlink.txt
rm -f geo.txt
rm -f gps-ops.txt
rm -f stations.txt

wget -O visual.txt   https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=tle
wget -O starlink.txt https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle
wget -O geo.txt      https://celestrak.org/NORAD/elements/gp.php?GROUP=geo&FORMAT=tle
wget -O gps-ops.txt  https://celestrak.org/NORAD/elements/gp.php?GROUP=gps-ops&FORMAT=tle
wget -O stations.txt https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle
