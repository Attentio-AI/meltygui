"""Small ctypes libwayland binding; xdg protocol metadata comes from bundled XML."""
import ctypes as C
from pathlib import Path
import xml.etree.ElementTree as ET

P = C.c_void_p
U = C.c_uint32
I = C.c_int32


class Interface(C.Structure):
    pass


class Message(C.Structure):
    _fields_ = [('name', C.c_char_p), ('signature', C.c_char_p),
                ('types', C.POINTER(C.POINTER(Interface)))]


Interface._fields_ = [('name', C.c_char_p), ('version', C.c_int),
                     ('method_count', C.c_int), ('methods', C.POINTER(Message)),
                     ('event_count', C.c_int), ('events', C.POINTER(Message))]


def function(lib, name, result, *args):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = result, list(args)
    return fn


class Native:
    def __init__(self):
        self.lib = C.CDLL('libwayland-client.so.0', use_errno=True)
        self.connect = function(self.lib, 'wl_display_connect', P, C.c_char_p)
        self.connect_fd = function(self.lib, 'wl_display_connect_to_fd', P, C.c_int)
        self.disconnect = function(self.lib, 'wl_display_disconnect', None, P)
        self.dispatch = function(self.lib, 'wl_display_dispatch', C.c_int, P)
        self.pending = function(self.lib, 'wl_display_dispatch_pending', C.c_int, P)
        self.prepare_read = function(self.lib, 'wl_display_prepare_read', C.c_int, P)
        self.read_events = function(self.lib, 'wl_display_read_events', C.c_int, P)
        self.cancel_read = function(self.lib, 'wl_display_cancel_read', None, P)
        self.roundtrip = function(self.lib, 'wl_display_roundtrip', C.c_int, P)
        self.flush = function(self.lib, 'wl_display_flush', C.c_int, P)
        self.get_fd = function(self.lib, 'wl_display_get_fd', C.c_int, P)
        self.get_version = function(self.lib, 'wl_proxy_get_version', U, P)
        self.destroy = function(self.lib, 'wl_proxy_destroy', None, P)
        self.listen = function(self.lib, 'wl_proxy_add_listener', C.c_int, P, C.POINTER(P), P)
        self.marshal = function(self.lib, 'wl_proxy_marshal_flags', P, P, U, C.POINTER(Interface), U, U)
        self.interfaces = {name: Interface.in_dll(self.lib, name + '_interface') for name in
                           ('wl_registry', 'wl_compositor', 'wl_surface', 'wl_seat',
                            'wl_pointer', 'wl_keyboard', 'wl_output', 'wl_shm', 'wl_shm_pool', 'wl_buffer',
                            'wl_data_device_manager', 'wl_data_device', 'wl_data_offer',
                            'wl_data_source')}
        self.keepalive = []
        directory = Path(__file__).with_name('protocols')
        nodes = [node for filename in ('xdg-shell.xml', 'xdg-decoration-unstable-v1.xml')
                 for node in ET.parse(directory / filename).getroot().findall('interface')]
        for node in nodes:
            self.interfaces[node.get('name')] = Interface()
        for node in nodes:
            interface = self.interfaces[node.get('name')]
            interface.name = node.get('name').encode()
            interface.version = 1  # All our xdg objects negotiate v1.
            for kind, count_field, messages_field in [('request', 'method_count', 'methods'),
                                                       ('event', 'event_count', 'events')]:
                messages = []
                for event in node.findall(kind):
                    args = event.findall('arg')
                    signature = (event.get('since', '') if event.get('since', '1') != '1' else '')
                    signature += ''.join(('?' if arg.get('allow-null') == 'true' else '') +
                                         {'int':'i', 'uint':'u', 'fixed':'f', 'string':'s',
                                          'object':'o', 'new_id':'n', 'array':'a', 'fd':'h'}[arg.get('type')]
                                         for arg in args)
                    types = (C.POINTER(Interface) * len(args))(*[
                        C.pointer(self.interfaces[arg.get('interface')]) if arg.get('interface') else None
                        for arg in args])
                    self.keepalive.append(types)
                    messages.append(Message(event.get('name').encode(), signature.encode(), types))
                table = (Message * len(messages))(*messages)
                self.keepalive.append(table)
                setattr(interface, count_field, len(messages))
                setattr(interface, messages_field, table)

    def request(self, proxy, opcode, *args, interface=None, version=None, destroy=False):
        result_interface = C.pointer(self.interfaces[interface]) if interface else None
        version = self.get_version(proxy) if version is None else version
        return self.marshal(proxy, opcode, result_interface, version, int(destroy), *args)

    def listener(self, proxy, callbacks, error_handler):
        functions = []
        for types, callback in callbacks:
            def invoke(data, obj, *args, _callback=callback):
                try:
                    _callback(*args)
                except BaseException as error:
                    error_handler(error)
            functions.append(C.CFUNCTYPE(None, P, P, *types)(invoke))
        table = (P * len(functions))(*[C.cast(fn, P) for fn in functions])
        self.keepalive.extend([functions, table])
        if self.listen(proxy, table, None) != 0:
            raise RuntimeError('wl_proxy_add_listener failed')
