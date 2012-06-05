#!/usr/bin/env python
import logging
import os, sys, time
import os.path
import threading
import marshal
import cPickle
import multiprocessing
import threading
import SocketServer
import SimpleHTTPServer
import shutil
import socket

import zmq
import mesos
import mesos_pb2

try:
    from setproctitle import setproctitle
except ImportError:
    def setproctitle(s):
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dpark.serialize import marshalable
from dpark.accumulator import Accumulator
from dpark.schedule import Success, OtherFailure
from dpark.env import env

logger = logging.getLogger("executor")

TASK_RESULT_LIMIT = 1024 * 256

Script = ''
Webport = None

def reply_status(driver, task, status, data=None):
    update = mesos_pb2.TaskStatus()
    update.task_id.value = task.task_id.value
    update.state = status
    if data is not None:
        update.data = data
    driver.sendStatusUpdate(update)

def run_task(task, aid):
    try:
        setproctitle('dpark worker %s: run task %s' % (Script, task))
        Accumulator.clear()
        result = task.run(aid)
        accUpdate = Accumulator.values()

        if marshalable(result):
            flag, data = 0, marshal.dumps(result)
        else:
            flag, data = 1, cPickle.dumps(result, -1)
        if len(data) > TASK_RESULT_LIMIT and env.dfs:
            workdir = env.get('WORKDIR')
            name = 'task_%s_%s.result' % (task.id, aid)
            path = os.path.join(workdir, name) 
            f = open(path, 'w')
            f.write(data)
            if env.dfs:
                f.flush()
                os.fsync(f.fileno())
            f.close()
            if env.dfs:
                data = "file://" + path
            else:
                data = "http://%s:%d/%s" % (socket.gethostname(), Webport, name)
            flag += 2

        setproctitle('dpark worker: idle')
        return mesos_pb2.TASK_FINISHED, cPickle.dumps((task.id, Success(), (flag, data), accUpdate), -1)
    except Exception, e:
        import traceback
        msg = traceback.format_exc()
        setproctitle('dpark worker: idle')
        return mesos_pb2.TASK_FAILED, cPickle.dumps((task.id, OtherFailure(msg), None, None), -1)

def init_env(args, port):
    setproctitle('dpark worker: idle')
    env.start(False, args, port=port)

basedir = None
class LocalizedHTTP(SimpleHTTPServer.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        out = SimpleHTTPServer.SimpleHTTPRequestHandler.translate_path(self, path)
        return basedir + '/' + out[len(os.getcwd()):]
    
    def log_message(self, format, *args):
        pass

def startWebServer(path):
    global basedir
    basedir = path
    ss = SocketServer.TCPServer(('0.0.0.0', 0), LocalizedHTTP)
    threading.Thread(target=ss.serve_forever).start()
    return ss.server_address[1]
    

def forword(fd, addr, prefix=''):
    f = os.fdopen(fd, 'r')
    ctx = zmq.Context()
    out = ctx.socket(zmq.PUSH)
    out.connect(addr)
    buf = []
    while True:
        try:
            line = f.readline()
            if not line: break
            buf.append(line)
            if line.endswith('\n'):
                out.send(prefix+''.join(buf))
                buf = []
        except IOError:
            break
    if buf:
        out.send(''.join(buf))
    out.close()
    f.close()
    ctx.shutdown()

def start_forword(addr, prefix=''):
    rfd, wfd = os.pipe()
    t = threading.Thread(target=forword, args=[rfd, addr, prefix])
    t.daemon = True
    t.start()    
    return t, os.fdopen(wfd, 'w', 0) 

class MyExecutor(mesos.Executor):
    def registered(self, driver, executorInfo, frameworkInfo, slaveInfo):
        try:
            global Script, Webport
            Script, cwd, python_path, parallel, out_logger, err_logger, logLevel, args = marshal.loads(executorInfo.data)
            try:
                os.chdir(cwd)
            except OSError:
                driver.sendFrameworkMessage("switch cwd failed: %s not exists!" % cwd)
            sys.path = python_path
            prefix = '[%s] ' % socket.gethostname()
            if out_logger:
                self.outt, sys.stdout = start_forword(out_logger, prefix)
            if err_logger:
                self.errt, sys.stderr = start_forword(err_logger, prefix)
            logging.basicConfig(format='%(asctime)-15s [%(name)-9s] %(message)s', level=logLevel)
            if args['DPARK_HAS_DFS'] == 'True':
                self.workdir = None
                port = None
            else:
                self.workdir = args['WORKDIR']
                Webport = startWebServer(args['WORKDIR'])
            self.pool = multiprocessing.Pool(parallel, init_env, [args, Webport])
            logger.debug("executor started at %s", slaveInfo.hostname)
        except Exception, e:
            import traceback
            msg = traceback.format_exc()
            driver.sendFrameworkMessage("init executor failed:\n " +  msg)

    def reregitered(self, driver, slaveInfo):
        logger.info("executor is reregistered at %s", slaveInfo.hostname)

    def disconnected():
        logger.info("executor is disconnected at %s", slaveInfo.hostname)

    def launchTask(self, driver, task):
        try:
            t, aid = cPickle.loads(task.data)
            
            def callback((state, data)):
                reply_status(driver, task, state, data)
        
            reply_status(driver, task, mesos_pb2.TASK_RUNNING)
            logging.debug("launch task %s", t.id) 
            self.pool.apply_async(run_task, [t, aid], callback=callback)
    
        except Exception, e:
            import traceback
            msg = traceback.format_exc()
            reply_status(driver, task, mesos_pb2.TASK_LOST, msg)
            return

    def killTask(self, driver, taskId):
        #driver.sendFrameworkMessage('kill task %s' % taskId)
        pass

    def shutdown(self, driver):
        # clean work files
        if self.workdir:
            try: shutil.rmtree(self.workdir, True)
            except: pass
        # flush
        sys.stdout.close()
        sys.stderr.close()
        self.outt.join()
        self.errt.join()
        for p in self.pool._pool:
            try: p.terminate()
            except: pass
        #for p in self.pool._pool:
        #    try: p.join()
        #    except: pass

    def error(self, driver, code, message):
        logger.error("error: %s, %s", code, message)

    def frameworkMessage(self, driver, data):
        pass

def run():
    executor = MyExecutor()
    driver = mesos.MesosExecutorDriver(executor)
    driver.run()

if __name__ == '__main__':
    run()
