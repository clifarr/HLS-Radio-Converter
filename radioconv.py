# -*- coding: utf-8 -*-

'''
    Project name: HLS radio stream converter
    File name: radioconv.py
    Author: Clifford Farrugia
    Date created: 27/08/2021
    Date last modified: 27/08/2021
    Python Version: 3.7
'''

ffmpeg_location = "ffmpeg" #replace with full exe path if using Windows such as "C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe"
ip_address = "x.x.x.x" #replace with your machine's private IP address
port = 8123
bitrate = 192000
buffer_size = 4096

import cherrypy
import subprocess
import threading
import m3u8
import time
import urllib.request
import urllib.parse

running_processes = {}

def download_thread(m3u8_url, ffmpeg_proc, reading_token):
    last_download = 0
    start_not_reading = time.time()
    current_reading = False
    playlist_duration = 0
    first_playlist = True
    last_sequence = 0
    while True:
        if reading_token.reading is not True:
            if current_reading:
                current_reading = False
                start_not_reading = time.time()
            else:
                if time.time() - start_not_reading > 10:
                    print ("closing thread 1")
                    try:
                        ffmpeg_proc.stdin.close()
                    except:
                        pass
                    return
            time.sleep(1)
            continue
        current_reading = True
        if time.time() - last_download < (playlist_duration - 2):
            print ("playlist duration not yet passed")
            time.sleep(1)
            continue
        last_download = time.time()
        playlist = m3u8.load(m3u8_url)
        playlist_duration = playlist.target_duration
        if first_playlist:
            first_playlist = False
            read_files = 2
        else:
            if playlist.media_sequence - last_sequence > 2:
                read_files = 1
            elif playlist.media_sequence - last_sequence == 0:
                time.sleep(1)
                continue
            else:                           
                read_files = playlist.media_sequence - last_sequence
        last_sequence = playlist.media_sequence
        for curr_file in range(read_files):
            orig_bytes = urllib.request.urlopen(playlist.files[curr_file - read_files]).read()
            current_loc = 0
            while current_loc < len(orig_bytes):
                if reading_token.reading is not True:
                    if current_reading:
                        current_reading = False
                        start_not_reading = time.time()
                    else:
                        if time.time() - start_not_reading > 10:
                            print ("closing thread 2")
                            try:
                                ffmpeg_proc.stdin.close()
                            except:
                                pass
                            return
                    time.sleep(1)
                    continue
                current_reading = True
                ffmpeg_proc.stdin.write(orig_bytes[current_loc:current_loc+buffer_size])
                current_loc += buffer_size


def cleanup_thread():
    while True:
        time.sleep(60)
        kill_processes = []
        for proc, proc_time in running_processes.items():
            if time.time() - proc_time > 15:
                kill_processes.append(proc)
        for kill_process in kill_processes:
            _ = running_processes.pop(kill_process)
            try:
                kill_process.kill()
            except:
                pass
    

class ReadingToken():
    reading = False


class ServerRoot:
    @cherrypy.expose
    def default(self, *args):
        print ("INCOMING!!")
        cherrypy.response.headers["Content-Type"] = "audio/mp3"

        def streamer():
            reading_token = ReadingToken()
            ffmpeg_proc = subprocess.Popen([ffmpeg_location, "-i", "pipe:", "-acodec", "mp3", "-b:a", str(bitrate), "-f", "mp3", "-"], stdout=subprocess.PIPE, stdin=subprocess.PIPE, bufsize=buffer_size)
            running_processes[ffmpeg_proc] = time.time()
            read_thread = threading.Thread(target=download_thread, args=[urllib.parse.unquote(args[0]), ffmpeg_proc, reading_token])
            read_thread.start()               
            while True:
                reading_token.reading = True
                running_processes[ffmpeg_proc] = time.time()
                read_data = ffmpeg_proc.stdout.read(buffer_size)
                if len(read_data) == 0:
                    break
                reading_token.reading = False
                yield (read_data)
        return streamer()

    default._cp_config = {'response.stream': True,
                          'engine.timeout_monitor.frequency': 1,
                          'response.timeout': 2}

threading.Thread(target=cleanup_thread).start()
cherrypy.server.socket_host = ip_address
cherrypy.server.socket_port = port
cherrypy.server.thread_pool = 3
cherrypy.quickstart(ServerRoot())
