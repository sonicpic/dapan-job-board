"""Read the typed binary response used by the public KDocs spreadsheet viewer."""
import struct

class BinVar:
    def __init__(self, data):
        self.data, self.pos, self.names = data, 0, {}
    def take(self, n):
        if n < 0 or self.pos + n > len(self.data): raise ValueError("Truncated KDocs response")
        v=self.data[self.pos:self.pos+n]; self.pos+=n; return v
    def num(self, fmt): return struct.unpack("<"+fmt,self.take(struct.calcsize(fmt)))[0]
    def value(self, t):
        fmts={1:"b",2:"B",3:"h",4:"H",5:"i",6:"I",7:"f",8:"d",10:"I",11:"d",19:"d"}
        if t in fmts: return self.num(fmts[t])
        if t==9: return bool(self.num("B"))
        if t==12: return self.take(self.num("I")*2).decode("utf-16-le")
        if t==13: return self.names[self.num("H")][0]
        if t==14: return self.obj()
        if t==17: return list(self.take(self.num("I")))
        if t==18:
            name=self.names[self.num("H")][0]; return {"structName":name,"data":self.obj()}
        if t==16:
            n=self.num("I")
            if not n:return []
            if n>1000000:raise ValueError("Unexpected array length")
            kind=self.num("H")
            return [self.element()[1] if kind in (14,16,18) else self.value(kind) for _ in range(n)]
        raise ValueError(f"Unknown type {t}")
    def element(self):
        key,t=self.names[self.num("H")]
        return (None,None) if t==15 else (key,self.value(t))
    def obj(self):
        result={}
        while self.pos<len(self.data):
            k,v=self.element()
            if k is None:break
            result[k]=v
        return result
    def read(self):
        for _ in range(self.num("H")):
            i=self.num("H");name=self.take(self.num("B")).decode("latin1");self.names[i]=(name,self.num("H"))
        return self.obj()
