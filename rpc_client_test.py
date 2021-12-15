#!/usr/bin/env python

'''Test client for the RPC library.'''

import sys

import rpc
print('hello')
sys.stdout.flush()
c = rpc.RpcClient('localhost:45678')
print('world')
sys.stdout.flush()
print(c.call('get_funs'))
sys.stdout.flush()
print(c.call('f'))
sys.stdout.flush()
print(c.call('e'))
sys.stdout.flush()

