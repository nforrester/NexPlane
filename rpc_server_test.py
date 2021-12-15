#!/usr/bin/env python

'''Test server for the RPC library.'''

import rpc
s = rpc.RpcServer(45678)
def e():
    raise Exception('fail')

s.add_fun_named('f', lambda: 'hello')
s.add_fun(e)
s.run()
