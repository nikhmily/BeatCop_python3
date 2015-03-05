#!/usr/bin/env python
## Python 2.7
"""Beatcop tries to ensure that a specified process runs on exactly one node in a cluster.
It does this by acquiring an expiring lock in Redis, which it then continually refreshes.
If the node stops refreshing its lock for any reason (like sudden death) another will acquire the lock and launch the specified process.

Beatcop is loosely based on the locking patterns described at http://redis.io/commands/set.
"""

import atexit
import ConfigParser
import logging
import os
import redis
import shlex
import signal
import socket
import subprocess
import sys
import time

try:
    import rediscluster

    # In redis-py-cluster 0.2.0 this hasn't been renamed yet. This check can go once a newer version is released.
    if hasattr(rediscluster, 'StrictRedisCluster'):
        RedisCluster = rediscluster.StrictRedisCluster
    else:
        RedisCluster = rediscluster.RedisCluster
except ImportError:
    pass


class Lock(object):
    """Lock class using Redis expiry."""

    lua_refresh = """
        if redis.call("get", KEYS[1]) == ARGV[1]
        then
            return redis.call("pexpire", KEYS[1], ARGV[2])
        else
            return 0
        end
    """

    def __init__(self, redis_, name, timeout=None, sleep=0.1):
        self.redis = redis_
        self.name = name
        self.timeout = timeout
        self.sleep = sleep
        # Instead of putting any old rubbish into the Lock's value, use our FQDN and PID
        self.value = "%s-%d" % (socket.getfqdn(), os.getpid())
        # rediscluster does not yet implement script management
        try:
            self._refresh_script = self.redis.register_script(self.lua_refresh)
        except rediscluster.exceptions.RedisClusterException:    # 'Method register_script is not possible to use in a redis cluster'
            pass

    def acquire(self, block=True):
        """Acquire lock. Blocks until acquired if `block` is `True`, otherwise returns `False` if the lock could not be acquired."""
        while True:
            # Try to set the lock
            if self.redis.set(self.name, self.value, px=self.timeout, nx=True):
                # It's ours until the timeout now
                return True
            # Lock is taken
            if not block:
                return False
            # If blocking, try again in a bit
            time.sleep(self.sleep)

    def refresh(self):
        """Refresh an existing lock to prevent it from expiring.
        Uses a LUA (EVAL) script to ensure only a lock which we own is being overwritten.
        Returns True if refresh succeeded, False if not."""
        keys = [self.name]
        args = [self.value, self.timeout]
        # Redis docs claim EVALs are atomic, and I'm inclined to believe it.
        if hasattr(self, '_refresh_script'):
            return self._refresh_script(keys=keys, args=args) == 1
        else:
            keys_and_args = keys + args
            return self.redis.eval(self.lua_refresh, len(keys), *keys_and_args)

    def who(self):
        """Returns the owner (value) of the lock or `None` if there isn't one."""
        return self.redis.get(self.name)


class BeatCop(object):
    """Run a process on a single node by using a Redis lock."""

    def __init__(self, command, redis_host_or_startup_nodes, redis_port=6379, redis_db=0, redis_password=None, lockname=None, timeout=1000, shell=False):
        self.command = command
        self.shell = shell
        self.timeout = timeout
        self.sleep = timeout / (1000.0 * 3)  # Convert to seconds and make sure we refresh at least 3 times per timeout period
        self.process = None
        redis_kwargs = dict(
            password=redis_password
        )
        if redis_db:
            redis_kwargs.update(dict(
                db=redis_db
            ))
        if isinstance(redis_host_or_startup_nodes, list):
            redis_kwargs.update(dict(
                startup_nodes=redis_host_or_startup_nodes
            ))
            log.info("BeatCop will connect to a Redis Cluster.")
        elif "/" in redis_host_or_startup_nodes:
            redis_kwargs.update(dict(
                unix_socket_path=redis_host_or_startup_nodes
            ))
            log.info("BeatCop will connect to single Redis instance via Unix domain socket.")
        else:
            redis_kwargs.update(dict(
                host=redis_host_or_startup_nodes,
                port=redis_port
            ))
            log.info("BeatCop will connect to single Redis instance via TCP.")
        self.redis = Redis(**redis_kwargs)
        try:
            redis_info = self.redis.info()
        except redis.exceptions.ConnectionError as e:
            log.error("Couldn't connect to Redis: %s", e.message)
            sys.exit(os.EX_NOHOST)
        # Check Redis version. The 'redis_version' key is absent in Redis Cluster, because redis_info is a dict of cluster nodes instead. In which case our Redis is definitely new enough.
        if 'redis_version' in redis_info and reduce(lambda l,r: l*1000+r, map(int,redis_info['redis_version'].split('.'))) < 2006012:
            log.error("Redis too old. You got %s, minimum requirement is %s", redis_info['redis_version'], '2.6.12')
            sys.exit(os.EX_PROTOCOL)
        self.lockname = lockname or ("beatcop:%s" % (self.command))
        self.lock = Lock(self.redis, self.lockname, timeout=self.timeout, sleep=self.sleep)

        atexit.register(self.crash)
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGHUP, self.handle_signal)

    def run(self):
        """Run process if nobody else is, otherwise wait until we're needed. Never returns."""

        log.info("Waiting for lock, currently held by %s", self.lock.who())
        if self.lock.acquire():
            log.info("Lock '%s' acquired", self.lockname)
            # We got the lock, so we make sure the process is running and keep refreshing the lock - if we ever stop for any reason, for example because our host died, the lock will soon expire.
            while True:
                if self.process is None:  # Process not spawned yet
                    self.process = self.spawn(self.command)
                    log.info("Spawned PID %d", self.process.pid)
                child_status = self.process.poll()
                if child_status is not None:
                    # Oops, process died on us.
                    log.error("Child died with exit code %d", child_status)
                    sys.exit(1)
                # Refresh lock and sleep
                if not self.lock.refresh():
                    who = self.lock.who()
                    if who is None:
                        if self.lock.acquire(block=False):
                            log.warning("Lock refresh failed, but successfully re-acquired unclaimed lock")
                        else:
                            log.error("Lock refresh and subsequent re-acquire failed, giving up (Lock now held by %s)", self.lock.who())
                            self.cleanup()
                            sys.exit(os.EX_UNAVAILABLE)
                    else:
                        log.error("Lock refresh failed, %s stole it - bailing out", self.lock.who())
                        self.cleanup()
                        sys.exit(os.EX_UNAVAILABLE)
                time.sleep(self.sleep)

    def spawn(self, command):
        """Spawn process."""
        if self.shell:
            args = command
        else:
            args = shlex.split(command)
        return subprocess.Popen(args, shell=self.shell)

    def cleanup(self):
        """Clean up, making sure the process is stopped before we pack up and go home."""
        if self.process is None:  # Process wasn't running yet, so nothing to worry about
            return
        if self.process.poll() is None:
            log.info("Sending TERM to %d", self.process.pid)
            self.process.terminate()
            # Give process a second to terminate, if it didn't, kill it.
            start = time.clock()
            while time.clock() - start < 1.0:
                time.sleep(0.05)
                if self.process.poll() is not None:
                    break
            else:
                log.info("Sending KILL to %d", self.process.pid)
                self.process.kill()
        assert self.process.poll() is not None

    def handle_signal(self, sig, frame):
        """Handles signals, surprisingly."""
        if sig in [signal.SIGINT]:
            log.warning("Ctrl-C pressed, shutting down...")
        if sig in [signal.SIGTERM]:
            log.warning("SIGTERM received, shutting down...")
        self.cleanup()
        sys.exit(-sig)

    def crash(self):
        """Handles unexpected exit, for example because Redis connection failed."""
        self.cleanup()


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print "Usage: %s <ini_file>" % sys.argv[0]
        sys.exit(os.EX_USAGE)
    config_file = sys.argv[1]

    logging.basicConfig(level=logging.INFO, format='%(asctime)s BeatCop: %(message)s', datefmt='%Y-%m-%d %H:%M:%S %Z')
    log = logging.getLogger()

    conf = ConfigParser.SafeConfigParser()
    conf.read(config_file)
    sections = conf.sections()

    # Config sanity check
    if conf.has_option('redis', 'host') and conf.has_option('redis', 'startup_nodes'):
        log.error("[redis] section of ini_file must specify one of 'host' or 'startup_nodes', not both.")
        sys.exit(os.EX_CONFIG)
    if not conf.has_option('redis', 'host') and not conf.has_option('redis', 'startup_nodes'):
        log.error("[redis] section of ini_file must specify either 'host' or 'startup_nodes' section. Didn't find either.")
        sys.exit(os.EX_CONFIG)

    if conf.has_option('redis', 'host'):
        # Single Redis mode
        redis_host_or_startup_nodes = conf.get('redis', 'host')
        Redis = redis.StrictRedis
    else:
        # Redis Cluster mode
        redis_host_or_startup_nodes = [dict(host=node.split(':')[0], port=node.split(':')[1]) for node in conf.get('redis', 'startup_nodes').split('\n')]
        Redis = RedisCluster

    beatcop_kwargs = dict(
        redis_host_or_startup_nodes=redis_host_or_startup_nodes,
        timeout=conf.getint('beatcop', 'timeout'),
        shell=conf.getboolean('beatcop', 'shell'),
    )
    if conf.has_option('redis', 'port'):
        beatcop_kwargs.update(dict(redis_port=conf.getint('redis', 'port')))
    if conf.has_option('redis', 'database'):
        beatcop_kwargs.update(dict(redis_db=conf.get('redis', 'database')))
    if conf.has_option('redis', 'password'):
        beatcop_kwargs.update(dict(redis_password=conf.get('redis', 'password')))
    if conf.has_option('beatcop', 'lockname'):
        beatcop_kwargs.update(dict(lockname=conf.get('beatcop', 'lockname')))
    beatcop = BeatCop(conf.get('beatcop', 'command'), **beatcop_kwargs)

    log.info("BeatCop starting on %s using lock '%s'", beatcop.lock.value, beatcop.lockname)
    beatcop.run()
