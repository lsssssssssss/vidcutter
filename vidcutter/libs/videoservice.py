#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#######################################################################
#
# VidCutter - media cutter & joiner
#
# copyright © 2017 Pete Alexandrou
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#######################################################################

import errno
import logging
import os
import re
import shlex
import sys
from bisect import bisect_left
from enum import Enum
from functools import partial

from PyQt5.QtCore import (pyqtSignal, pyqtSlot, QDir, QFile, QFileInfo, QObject, QProcess, QProcessEnvironment, QSize,
                          QStandardPaths, QStorageInfo, QTemporaryFile, QTime)
from PyQt5.QtGui import QPainter, QPixmap
from PyQt5.QtWidgets import QMessageBox

from vidcutter.libs.munch import Munch
from vidcutter.libs.videoconfig import FFmpegNotFoundException, InvalidMediaException, VideoConfig

try:
    # noinspection PyPackageRequirements
    import simplejson as json
except ImportError:
    import json


class VideoService(QObject):
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)
    error = pyqtSignal(str)

    frozen = getattr(sys, 'frozen', False)
    spaceWarningThreshold = 200
    spaceWarningDelivered = False
    smartcutError = False

    class ThumbSize(Enum):
        INDEX = QSize(100, 70)
        TIMELINE = QSize(105, 60)

    class StreamType(Enum):
        AUDIO = 0
        VIDEO = 1
        SUBTITLE = 2

    config = VideoConfig()

    def __init__(self, parent=None, ffmpegpath: str=None):
        super(VideoService, self).__init__(parent)
        self.parent = parent
        self.ffmpegpath = ffmpegpath
        self.logger = logging.getLogger(__name__)
        try:
            self.backends = VideoService.findBackends(self.ffmpegpath)
            if self.backends.ffmpeg is not None and self.parent is not None:
                self.parent.ffmpegPath = self.backends.ffmpeg
                self.parent.settings.setValue('ffmpegPath', os.path.dirname(self.backends.ffmpeg))
            self.proc = VideoService.initProc()
            if hasattr(self.proc, 'errorOccurred'):
                self.proc.errorOccurred.connect(self.cmdError)
            self.lastError = ''
            self.media, self.source = None, None
            self.keyframes = []
            self.streams = Munch()
        except FFmpegNotFoundException as e:
            self.logger.exception(e.msg, exc_info=True)
            QMessageBox.critical(getattr(self, 'parent', None), 'Missing libraries', e.msg, QMessageBox.Ok)

    def setMedia(self, source: str) -> None:
        try:
            self.source = source
            self.probe(source)
            if self.media is not None:
                if getattr(self.parent, 'verboseLogs', False):
                    self.logger.info(self.media)
                for codec_type in VideoService.StreamType.__members__:
                    setattr(self.streams, codec_type.lower(),
                            [stream for stream in self.media.streams if stream.codec_type == codec_type.lower()])
                if len(self.streams.video):
                    self.streams.video = self.streams.video[0]  # we always assume one video stream per media file
                else:
                    raise InvalidMediaException('Could not load video stream for {}'.format(source))
        except OSError as e:
            if e.errno == errno.ENOENT:
                errormsg = '{0}: {1}'.format(os.strerror(errno.ENOENT), source)
                self.logger.error(errormsg)
                raise FileNotFoundError(errormsg)

    @staticmethod
    def findBackends(ffmpegpath: str=None) -> Munch:
        tools = Munch(ffmpeg=None, ffprobe=None, mediainfo=None)
        if ffmpegpath is not None and sys.platform.startswith('linux') and not os.getenv('QT_APPIMAGE', False):
            for exe in VideoService.config.binaries['posix']['ffmpeg']:
                binpath = os.path.join(ffmpegpath, exe)
                if binpath is not None and os.path.isfile(binpath):
                    tools['ffmpeg'] = binpath
                    break
            for exe in VideoService.config.binaries['posix']['ffprobe']:
                binpath = os.path.join(ffmpegpath, exe)
                if binpath is not None and os.path.isfile(binpath):
                    tools['ffprobe'] = binpath
                    break
        for backend in tools.keys():
            for exe in VideoService.config.binaries[os.name][backend]:
                if tools[backend] is None:
                    binpath = QDir.toNativeSeparators('{0}/bin/{1}'.format(VideoService.getAppPath(), exe))
                    if binpath is not None and os.path.isfile(binpath):
                        tools[backend] = binpath
                        break
                    else:
                        binpath = QStandardPaths.findExecutable(exe)
                        if binpath is not None and os.path.isfile(binpath):
                            tools[backend] = binpath
                            break
        if tools.ffmpeg is None:
            raise FFmpegNotFoundException('Could not locate ffmpeg/avconv on your system')
        if tools.ffprobe is None:
            raise FFmpegNotFoundException('Could not locate ffprobe/avprobe on your system')
        return tools

    @staticmethod
    def initProc(program: str=None, finish: pyqtSlot=None, workingdir: str=None) -> QProcess:
        p = QProcess()
        p.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
        p.setProcessChannelMode(QProcess.MergedChannels)
        p.setWorkingDirectory(workingdir if workingdir is not None else VideoService.getAppPath())
        if program is not None:
            p.setProgram(program)
        if finish is not None:
            p.finished.connect(finish)
        return p

    def checkDiskSpace(self, path: str):
        # noinspection PyCallByClass
        if self.spaceWarningDelivered or not QFileInfo.exists(path):
            return
        info = QStorageInfo(path)
        available = info.bytesAvailable() / 1000 / 1000
        if available < VideoService.spaceWarningThreshold:
            warnmsg = 'There is less than {0}MB of disk space available at the target folder selected to save ' \
                      'your media. VidCutter WILL FAIL to produce your media if you run out of space during ' \
                      'operations.'
            QMessageBox.warning(self.parentWidget(), 'Disk space warning',
                                warnmsg.format(VideoService.spaceWarningThreshold))
            self.spaceWarningDelivered = True

    @staticmethod
    def captureFrame(source: str, frametime: str, thumbsize: QSize=ThumbSize.INDEX.value,
                     external: bool=False) -> QPixmap:
        # print('[captureFrame] frametime: {0}  thumbsize: {1}'.format(frametime, thumbsize))
        capres = QPixmap()
        img = QTemporaryFile(os.path.join(QDir.tempPath(), 'XXXXXX.jpg'))
        if img.open():
            imagecap = img.fileName()
            cmd = VideoService.findBackends().ffmpeg
            args = '-hide_banner -ss {0} -i "{1}" -vframes 1 -s {2:d}x{3:d} -y "{4}"' \
                   .format(frametime, source, thumbsize.width(), thumbsize.height(), imagecap)
            proc = VideoService.initProc()
            if proc.state() == QProcess.NotRunning:
                # if os.getenv('DEBUG', False):
                #     logging.getLogger(__name__).info('"%s %s"' % (cmd, args))
                proc.start(cmd, shlex.split(args))
                proc.waitForFinished(-1)
                if proc.exitStatus() == QProcess.NormalExit and proc.exitCode() == 0:
                    capres = QPixmap(imagecap, 'JPG')
                if external:
                    painter = QPainter(capres)
                    painter.drawPixmap(0, 0, QPixmap(':/images/external.png', 'PNG'))
                    painter.end()
        img.remove()
        return capres

    # noinspection PyBroadException
    def testJoin(self, file1: str, file2: str) -> bool:
        result = False
        self.logger.info('attempting to test joining of "{0}" & "{1}"'.format(file1, file2))
        try:
            # 1. check audio + video codecs
            file1_codecs = self.codecs(file1)
            file2_codecs = self.codecs(file2)
            if file1_codecs != file2_codecs:
                self.logger.info('join test failed for {0} and {1}: codecs mismatched'.format(file1, file2))
                self.lastError = '<p>The audio + video format of this media file is not the same as the files ' \
                                 'already in your clip index.</p>' \
                                 '<div align="center">Current files are <b>{0}</b> (video) and ' \
                                 '<b>{1}</b> (audio)<br/>' \
                                 'Failed media is <b>{2}</b> (video) and <b>{3}</b> (audio)</div>'
                self.lastError = self.lastError.format(file1_codecs[0], file1_codecs[1],
                                                       file2_codecs[0], file2_codecs[1])
                return result
            # 2. check frame sizes
            size1 = self.framesize(file1)
            size2 = self.framesize(file2)
            if size1 != size2:
                self.logger.info('join test failed for {0} and {1}: frame size mismatched'.format(file1, file2))
                self.lastError = '<p>The frame size of this media file is not the same as the files already in ' \
                                 'your clip index.</p>' \
                                 '<div align="center">Current media clips are <b>{0}x{1}</b>' \
                                 '<br/>Failed media file is <b>{2}x{3}</b></div>'
                self.lastError = self.lastError.format(size1.width(), size1.height(), size2.width(), size2.height())
                return result
            # 2. generate temporary file handles
            _, ext = os.path.splitext(file1)
            file1_cut = QTemporaryFile(os.path.join(QDir.tempPath(), 'XXXXXX{}'.format(ext)))
            file2_cut = QTemporaryFile(os.path.join(QDir.tempPath(), 'XXXXXX{}'.format(ext)))
            final_join = QTemporaryFile(os.path.join(QDir.tempPath(), 'XXXXXX{}'.format(ext)))
            # 3. produce 4 secs clips from input files for join test
            if file1_cut.open() and file2_cut.open() and final_join.open():
                result1 = self.cut(file1, file1_cut.fileName(), '00:00:00.000', '00:00:04.00', False)
                result2 = self.cut(file2, file2_cut.fileName(), '00:00:00.000', '00:00:04.00', False)
                if result1 and result2:
                    # 4. attempt join of temp 2 second clips
                    result = self.join([file1_cut.fileName(), file2_cut.fileName()], final_join.fileName(), False)
            VideoService.cleanup([file1_cut.fileName(), file2_cut.fileName(), final_join.fileName()])
        except BaseException:
            self.logger.exception('Exception in VideoService.testJoin', exc_info=True)
            result = False
        return result

    def framesize(self, source: str=None) -> QSize:
        if source is None and hasattr(self.streams, 'video'):
            return QSize(int(self.streams.video.width), int(self.streams.video.height))
        else:
            args = '-i "{}"'.format(source)
            result = self.cmdExec(self.backends.ffmpeg, args, True)
            matches = re.search(r'Stream.*Video:.*[,\s](?P<width>\d+?)x(?P<height>\d+?)[,\s]',
                                result, re.DOTALL).groupdict()
            return QSize(int(matches['width']), int(matches['height']))

    def duration(self, source: str=None) -> QTime:
        if source is None and hasattr(self.media, 'format') and self.parent is not None:
            return self.parent.delta2QTime(float(self.media.format.duration) * 1000)
        else:
            args = '-i "{}"'.format(source)
            result = self.cmdExec(self.backends.ffmpeg, args, True)
            matches = re.search(r'Duration:\s(?P<hrs>\d+?):(?P<mins>\d+?):(?P<secs>\d+\.\d+?),',
                                result, re.DOTALL).groupdict()
            secs, msecs = matches['secs'].split('.')
            return QTime(int(matches['hrs']), int(matches['mins']), int(secs), int(msecs))

    def codecs(self, source: str=None) -> tuple:
        if source is None and hasattr(self.streams, 'video'):
            return self.streams.video.codec_name, self.streams.audio[0].codec_name if len(self.streams.audio) else None
        else:
            args = '-i "{}"'.format(source)
            result = self.cmdExec(self.backends.ffmpeg, args, True)
            vcodec = re.search(r'Stream.*Video:\s(\w+)', result).group(1)
            acodec = re.search(r'Stream.*Audio:\s(\w+)', result).group(1)
            return vcodec, acodec

    def cut(self, source: str, output: str, frametime: str, duration: str, allstreams: bool=True, vcodec: str=None,
            run: bool=True):
        self.checkDiskSpace(output)
        source = QDir.toNativeSeparators(source)
        stream_map = '-map 0 ' if allstreams else ''
        if vcodec is not None:
            encode_options = VideoService.config.encoding.get(vcodec, vcodec)
            args = '-v 32 -i "{}" -ss {} -t {} -c:v {} -c:a copy -c:s copy {}-avoid_negative_ts 1 ' \
                   '-copyinkf -y "{}"'.format(source, frametime, duration, encode_options, stream_map, output)
        else:
            args = '-v error -ss {} -t {} -i "{}" -c copy {}-avoid_negative_ts 1 -copyinkf -y "{}"' \
                   .format(frametime, duration, source, stream_map, output)
        if run:
            result = self.cmdExec(self.backends.ffmpeg, args)
            if not result or os.path.getsize(output) < 1000:
                if allstreams:
                    # cut failed so try again without mapping all media streams
                    self.logger.info('cut resulted in zero length file, trying again without all stream mapping')
                    self.cut(source, output, frametime, duration, False)
                else:
                    # both attempts to cut have failed so exit and let user know
                    VideoService.cleanup([output])
                    return False
            return True
        else:
            if os.getenv('DEBUG', False) or getattr(self.parent, 'verboseLogs', False):
                self.logger.info(args)
            return args

    def smartinit(self, clips: int):
        self.smartcut_jobs = []
        # noinspection PyUnusedLocal
        [
            self.smartcut_jobs.append(Munch(output='', bitrate=0, allstreams=True, procs={}, files={}, results={}))
            for index in range(clips)
        ]

    def smartcut(self, index: int, source: str, output: str, start: float, end: float, allstreams: bool=True) -> None:
        source = QDir.toNativeSeparators(source)
        output_file, output_ext = os.path.splitext(output)
        bisections = self.getGOPbisections(source, start, end)
        self.smartcut_jobs[index].output = output
        self.smartcut_jobs[index].allstreams = allstreams
        # ----------------------[ STEP 1 - start of clip if not starting on a keyframe ]-------------------------
        if bisections['start'][1] > bisections['start'][0]:
            self.smartcut_jobs[index].files.update(start='{0}_start_{1}{2}'
                                                   .format(output_file, '{0:0>2}'.format(index), output_ext))
            startproc = VideoService.initProc(self.backends.ffmpeg, self.smartcheck, os.path.dirname(source))
            startproc.setObjectName('start.{}'.format(index))
            startproc.started.connect(lambda: self.progress.emit(index))
            startproc.setArguments(shlex.split(
                self.cut(source=source,
                         output=self.smartcut_jobs[index].files['start'],
                         frametime=str(start),
                         duration=bisections['start'][1] - start,
                         allstreams=allstreams,
                         vcodec=self.streams.video.codec_name,
                         run=False)))
            self.smartcut_jobs[index].procs.update(start=startproc)
            self.smartcut_jobs[index].results.update(start=False)
            startproc.start()
        # ----------------------[ STEP 2 - cut middle segment of clip ]-------------------------
        self.smartcut_jobs[index].files.update(middle='{0}_middle_{1}{2}'
                                               .format(output_file, '{0:0>2}'.format(index), output_ext))
        middleproc = VideoService.initProc(self.backends.ffmpeg, self.smartcheck, os.path.dirname(source))
        middleproc.setProcessChannelMode(QProcess.MergedChannels)
        middleproc.setWorkingDirectory(os.path.dirname(self.smartcut_jobs[index].files['middle']))
        middleproc.setObjectName('middle.{}'.format(index))
        middleproc.started.connect(lambda: self.progress.emit(index))
        middleproc.setArguments(shlex.split(
            self.cut(source=source,
                     output=self.smartcut_jobs[index].files['middle'],
                     frametime=bisections['start'][2],
                     duration=bisections['end'][1] - bisections['start'][2],
                     allstreams=allstreams,
                     run=False)))
        self.smartcut_jobs[index].procs.update(middle=middleproc)
        self.smartcut_jobs[index].results.update(middle=False)
        if len(self.smartcut_jobs[index].procs) == 1:
            middleproc.start()
        # ----------------------[ STEP 3 - end of clip if not ending on a keyframe ]-------------------------
        if bisections['end'][2] > bisections['end'][1]:
            self.smartcut_jobs[index].files.update(end='{0}_end_{1}{2}'
                                                   .format(output_file, '{0:0>2}'.format(index), output_ext))
            endproc = VideoService.initProc(self.backends.ffmpeg, self.smartcheck, os.path.dirname(source))
            endproc.setObjectName('end.{}'.format(index))
            endproc.started.connect(lambda: self.progress.emit(index))
            endproc.setArguments(shlex.split(
                self.cut(source=source,
                         output=self.smartcut_jobs[index].files['end'],
                         frametime=bisections['end'][1],
                         duration=end - bisections['end'][1],
                         allstreams=allstreams,
                         vcodec=self.streams.video.codec_name,
                         run=False)))
            self.smartcut_jobs[index].procs.update(end=endproc)
            self.smartcut_jobs[index].results.update(end=False)
            # endproc.start()

    @pyqtSlot(int, QProcess.ExitStatus)
    def smartcheck(self, code: int, status: QProcess.ExitStatus) -> None:
        if hasattr(self, 'smartcut_jobs') and not self.smartcutError:
            name, index = self.sender().objectName().split('.')
            index = int(index)
            self.smartcut_jobs[index].results[name] = (code == 0 and status == QProcess.NormalExit)
            if os.getenv('DEBUG', False) or getattr(self.parent, 'verboseLogs', False):
                self.logger.info('SmartCut progress: {}'.format(self.smartcut_jobs[index].results))
            resultfile = self.smartcut_jobs[index].files.get(name)
            if not self.smartcut_jobs[index].results[name] or os.path.getsize(resultfile) < 1000:
                args = self.smartcut_jobs[index].procs[name].arguments()
                if '-map' in args:
                    self.logger.info('SmartCut resulted in zero length file, trying again without all stream mapping')
                    pos = args.index('-map')
                    args.remove('-map')
                    del args[pos]
                    self.smartcut_jobs[index].procs[name].setArguments(args)
                    self.smartcut_jobs[index].procs[name].started.disconnect()
                    self.smartcut_jobs[index].procs[name].start()
                    return
                else:
                    self.smartcutError = True
                    # both attempts to cut have failed so exit and let user know
                    self.logger.error('Error executing: {0} {1}'
                                      .format(self.smartcut_jobs[index].procs[name].program(), args))
                    self.error.emit('SmartCut failed to cut media file. Please ensure your media files are valid '
                                    'otherwise try again with SmartCut disabled.')
                    return
            if False not in self.smartcut_jobs[index].results.values():
                self.smartjoin(index)
            else:
                if name == 'start':
                    self.smartcut_jobs[index].procs['middle'].start()
                elif name == 'middle':
                    self.smartcut_jobs[index].procs['end'].start()

    def smartabort(self):
        for job in self.smartcut_jobs:
            for name in job.procs:
                if job.procs[name].state() != QProcess.NotRunning:
                    job.procs[name].terminate()
            VideoService.cleanup(job.files)

    def smartjoin(self, index: int) -> None:
        self.progress.emit(index)
        final_join = False
        joinlist = [
            self.smartcut_jobs[index].files.get('start'),
            self.smartcut_jobs[index].files.get('middle'),
            self.smartcut_jobs[index].files.get('end')
        ]
        if self.isMPEGcodec(joinlist[1]):
            self.logger.info('smartcut files are MPEG based so join via MPEG-TS')
            final_join = self.mpegtsJoin(joinlist, self.smartcut_jobs[index].output)
        if not final_join:
            self.logger.info('smartcut MPEG-TS join failed, retry with standard concat')
            final_join = self.join(inputs=joinlist, output=self.smartcut_jobs[index].output,
                                   allstreams=self.smartcut_jobs[index].allstreams)
        VideoService.cleanup(joinlist)
        self.finished.emit(final_join, self.smartcut_jobs[index].output)

    @staticmethod
    def cleanup(files: list) -> None:
        try:
            [os.remove(file) for file in files]
        except FileNotFoundError:
            pass

    def join(self, inputs: list, output: str, allstreams: bool=True) -> bool:
        self.checkDiskSpace(output)
        filelist = os.path.normpath(os.path.join(os.path.dirname(inputs[0]), '_vidcutter.list'))
        fobj = open(filelist, 'w')
        [fobj.write('file \'%s\'\n' % file.replace("'", "\\'")) for file in inputs]
        fobj.close()
        stream_map = '-map 0 ' if allstreams else ''
        args = '-v error -f concat -segment_time_metadata 1 -safe 0 -i "{}" -c copy {}-y "{}"'
        result = self.cmdExec(self.backends.ffmpeg, args.format(filelist, stream_map, output))
        os.remove(filelist)
        return result

    def getBSF(self, source: str) -> tuple:
        vbsf, absf = '', ''
        vcodec, acodec = self.codecs(source)
        if vcodec:
            prefix = '-bsf:v'
            if vcodec == 'hevc':
                vbsf = '{} hevc_mp4toannexb'.format(prefix)
            elif vcodec == 'h264':
                vbsf = '{} h264_mp4toannexb'.format(prefix)
            elif vcodec == 'mpeg4':
                vbsf = '{} mpeg4_unpack_bframes'.format(prefix)
            elif vcodec in {'webm', 'ivf', 'vp9'}:
                vbsf = '{} vp9_superframe'.format(prefix)
        if acodec:
            prefix = '-bsf:a'
            if acodec == 'aac':
                absf = '{} aac_adtstoasc'.format(prefix)
            elif acodec == 'mp3':
                absf = '{} mp3decomp'.format(prefix)
        return vbsf, absf

    def probe(self, source: str) -> bool:
        try:
            args = '-v error -show_streams -show_format -of json "{}"'.format(source)
            json_data = self.cmdExec(self.backends.ffprobe, args, output=True)
            self.media = Munch.fromDict(json.loads(json_data))
            return hasattr(self.media, 'streams') and len(self.media.streams)
        except FileNotFoundError:
            self.logger.exception('Probe media file not found: {}'.format(source), exc_info=True)
            raise
        except json.JSONDecodeError:
            self.logger.exception('Error decoding ffprobe JSON output', exc_info=True)
            raise

    def getKeyframes(self, source: str, formatted_time: bool=False) -> list:
        if len(self.keyframes) and source == self.source:
            return self.keyframes
        timecode = '0:00:00.000000' if formatted_time else 0
        args = '-v error -show_packets -select_streams v -show_entries packet=pts_time,flags ' \
               '{0}-of csv "{1}"'.format('-sexagesimal ' if formatted_time else '', source)
        result = self.cmdExec(self.backends.ffprobe, args, output=True, suppresslog=True)
        keyframe_times = []
        # print('keyframes: {}'.format(result))
        for line in result.split('\n'):
            if line.split(',')[1] != 'N/A':
                timecode = line.split(',')[1]
            if re.search(',K', line):
                if formatted_time:
                    keyframe_times.append(timecode[:-3])
                else:
                    keyframe_times.append(float(timecode))
        last_keyframe = self.duration().toString('h:mm:ss.zzz')
        if keyframe_times[-1] != last_keyframe:
            keyframe_times.append(last_keyframe)
        if source == self.source and not formatted_time:
            self.keyframes = keyframe_times
        return keyframe_times

    def getGOPbisections(self, source: str, start: float, end: float) -> dict:
        keyframes = self.getKeyframes(source)
        start_pos = bisect_left(keyframes, start)
        end_pos = bisect_left(keyframes, end)
        return {
            'start': (
                keyframes[start_pos - 1] if start_pos > 0 else keyframes[start_pos],
                keyframes[start_pos],
                keyframes[start_pos + 1]
            ),
            'end': (
                keyframes[end_pos - 2] if end_pos != (len(keyframes) - 1) else keyframes[end_pos - 1],
                keyframes[end_pos - 1] if end_pos != (len(keyframes) - 1) else keyframes[end_pos],
                keyframes[end_pos]
            )
        }

    def isMPEGcodec(self, source: str=None) -> bool:
        if source is None and hasattr(self.streams, 'video'):
            codec = self.streams.video.codec_name
        else:
            codec = self.codecs(source)[0].lower()
        return codec in VideoService.config.mpeg_formats

    # noinspection PyBroadException
    def mpegtsJoin(self, inputs: list, output: str) -> bool:
        result = False
        try:
            self.checkDiskSpace(output)
            outfiles = []
            video_bsf, audio_bsf = self.getBSF(inputs[0])
            # 1. transcode to mpeg transport streams
            for file in inputs:
                name, _ = os.path.splitext(file)
                outfile = '{}.ts'.format(name)
                outfiles.append(outfile)
                if os.path.isfile(outfile):
                    os.remove(outfile)
                args = '-v error -i "{0}" -c copy -map 0 {1} -f mpegts "{2}"'.format(file, video_bsf, outfile)
                if not self.cmdExec(self.backends.ffmpeg, args):
                    return result
            # 2. losslessly concatenate at the file level
            if len(outfiles):
                if os.path.isfile(output):
                    os.remove(output)
                args = '-v error -i "concat:{0}" -c copy {1} "{2}"' \
                    .format("|".join(map(str, outfiles)), audio_bsf, output)
                result = self.cmdExec(self.backends.ffmpeg, args)
                # 3. cleanup mpegts files
                [QFile.remove(file) for file in outfiles]
        except BaseException:
            self.logger.exception('Exception during MPEG-TS join', exc_info=True)
            result = False
        return result

    def version(self) -> str:
        args = '-version'
        result = self.cmdExec(self.backends.ffmpeg, args, True)
        return re.search(r'ffmpeg\sversion\s([\S]+)\s', result).group(1)

    def mediainfo(self, source: str, output: str='HTML') -> str:
        args = '--output={0} "{1}"'.format(output, source)
        result = self.cmdExec(self.backends.mediainfo, args, True, True)
        return result.strip()

    def cmdExec(self, cmd: str, args: str=None, output: bool=False, suppresslog: bool=False):
        if self.proc.state() == QProcess.NotRunning:
            if cmd == self.backends.mediainfo:
                self.proc.setProcessChannelMode(QProcess.SeparateChannels)
            if cmd in {self.backends.ffmpeg, self.backends.ffprobe}:
                args = '-hide_banner {}'.format(args)
            if os.getenv('DEBUG', False) or getattr(self.parent, 'verboseLogs', False):
                self.logger.info('{0} {1}'.format(cmd, args if args is not None else ''))
            self.proc.start(cmd, shlex.split(args))
            self.proc.readyReadStandardOutput.connect(
                partial(self.cmdOut, self.proc.readAllStandardOutput().data().decode().strip()))
            self.proc.waitForFinished(-1)
            if cmd == self.backends.mediainfo:
                self.proc.setProcessChannelMode(QProcess.MergedChannels)
            if output:
                cmdoutput = self.proc.readAllStandardOutput().data().decode().strip()
                if getattr(self.parent, 'verboseLogs', False) and not suppresslog:
                    self.logger.info('cmd output: {}'.format(cmdoutput))
                return cmdoutput
            return self.proc.exitStatus() == QProcess.NormalExit and self.proc.exitCode() == 0
        return False

    @pyqtSlot(str)
    def cmdOut(self, output: str) -> None:
        if len(output):
            self.logger.info(output)

    @pyqtSlot(QProcess.ProcessError)
    def cmdError(self, error: QProcess.ProcessError) -> None:
        if error != QProcess.Crashed:
            QMessageBox.critical(self.parent, 'Error alert',
                                 '<h4>{0} Error:</h4><p>{1}</p>'.format(self.backends.ffmpeg, self.proc.errorString()),
                                 buttons=QMessageBox.Close)

    # noinspection PyUnresolvedReferences, PyProtectedMember
    @staticmethod
    def getAppPath() -> str:
        if VideoService.frozen:
            return sys._MEIPASS
        return os.path.dirname(os.path.realpath(sys.argv[0]))
