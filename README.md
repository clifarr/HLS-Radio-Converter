# HLS converter for Frontier Silicon and other Internet radios

The aim of this project is to provide a very simple script that can convert an 
HLS stream to MP3 so that it can be played by Internet Radios based on Frontier
Silicon and other similar radios that do not support HLS streams. It is meant
to be run on simple machines such as Raspberry Pi but it may also be used on
Windows, Linux, and probably Mac (not tested on Mac).

## Requirements

* Python 3 (such as Python 3.7)
* Cherrypy (Python library - may be installed by using pip.
    * On Raspberry Pi, the pip executable for Python 3 is named "pip3"
    * Example command: pip3 install cherrypy
* ffmpeg

## Setting Up

1. Edit the file to set the ffmpeg location (if using Windows) and IP address.
1. If running on Linux, it's best to set this up as a service. Example service file attached.
1. If running on Windows, you may need to allow port 8123 (or the port number you set) as it is blocked by Windows Firewall by default.

## Using

* The stream URL that you shall set your radio to play is in the format http://x.x.x.x:8123/<encoded_original_url>
* Replace x.x.x.x with your machine's IP address
* The original m3u8 URL needs to be URL encoded. You may use the following site to encode: https://meyerweb.com/eric/tools/dencoder/
* Example:
    * Original URL: https://radiom2o-lh.akamaihd.net/i/RadioM2o_Live_1@42518/index_96_a-p.m3u8?sd=10&rebase=on
    * URL for your radio: http://192.168.1.5:8123/https%3A%2F%2Fradiom2o-lh.akamaihd.net%2Fi%2FRadioM2o_Live_1%4042518%2Findex_96_a-p.m3u8%3Fsd%3D10%26rebase%3Don
