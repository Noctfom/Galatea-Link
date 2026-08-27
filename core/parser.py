# core/parser.py
import io
from core.gamestate import MessageParser

class OCGParser:
    """
    负责处理底层 TCP 粘包、拆包，以及拦截未知指令的黑洞雷达
    """
    @staticmethod
    def robust_parse(data):
        msgs = []
        stream = io.BytesIO(data)
        data_len = len(data)
        last_pos = -1
        
        while stream.tell() < data_len:
            start = stream.tell()

            # --- 死循环雷达 ---
            if start == last_pos:
                stream.seek(start)
                stuck_type = stream.read(1)[0]
                print(f"\n🚨 [死循环拦截] 解析器卡死在未知 OCG Type: {stuck_type}")
                print(f"📦 完整数据包 Hex: {data.hex()}")
                break 
            last_pos = start

            start_pos = stream.tell()
            msg_type = data[start_pos]
            stream.read(1) # 跳过 msg_type 字节
            
            # 特殊指令长度
            if msg_type in [4, 6, 8]:
                length = data_len - start_pos - 1
            else:
                length = MessageParser.MSG_LEN.get(msg_type, -1)
                if length == -1:
                    try:
                        length = MessageParser.calculate_dynamic_length(msg_type, stream)
                    except Exception as e:
                        # 记录异常，防止被静音吞噬
                        print(f"⚠️ 动态长度计算异常 (Type {msg_type}): {e}")
                        length = -1
            
            # --- 黑洞截断警告雷达 ---
            if length < 0 or stream.tell() + length > data_len:
                print(f"\n🚨 [黑洞截断警告] 未知 OCG 指令 (Type {msg_type}) 缺失长度定义！")
                print(f"💥 强行吞噬了剩余的 {data_len - start_pos - 1} 个字节，这将导致后续时点丢失卡死！")
                print(f"📦 吞噬残骸 Hex: {data[start_pos:].hex()}")
                
                length = data_len - start_pos - 1
                
            payload = stream.read(length)
            
            # 返回拆解好的 tuple: (类型, 载荷)
            msgs.append((msg_type, payload))
            
        return msgs