# 整型响应编码，统一有符号数和完整三十二位掩码的网络字节表示

from numbers import Integral
import struct


# 接受有符号响应与无符号位掩码，拒绝超出四字节的数值且不静默截断
def encode_int_response(value):
    if not isinstance(value, Integral) or not -(1 << 31) <= value <= 0xFFFFFFFF:
        raise ValueError("整型响应必须位于有符号或无符号三十二位范围")
    return struct.pack("<I", int(value) & 0xFFFFFFFF)
