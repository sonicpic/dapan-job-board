import datetime as dt
import hashlib
import http.cookiejar
import json
import os
import re
import unicodedata
import urllib.request
from urllib.parse import urlsplit
from binvar import BinVar

SOURCE=os.getenv('KDOCS_SOURCE_URL','https://www.kdocs.cn/l/EXAMPLE')
TZ=dt.timezone(dt.timedelta(hours=8))

def normalize_date(value, year, with_time=False):
    if isinstance(value,(int,float)) and 20000<value<100000:
        d=dt.datetime(1899,12,30)+dt.timedelta(seconds=round(value*86400))
        return d.strftime('%Y-%m-%dT%H:%M:00+08:00' if with_time else '%Y-%m-%d')
    text=unicodedata.normalize('NFKC',str(value or '')).strip()
    m=re.search(r'(?:(20\d{2})[年./-])?(\d{1,2})[月./-](\d{1,2})(?:日)?',text)
    if not m:return None
    try:d=dt.datetime(int(m[1] or year),int(m[2]),int(m[3]),tzinfo=TZ)
    except ValueError:return None
    if not with_time:return d.date().isoformat()
    times=re.findall(r'(\d{1,2})\s*:\s*(\d{2})',text[m.end():])
    if times:
        h,minute=map(int,times[0])
        if any(w in text for w in ['下午','晚上']) and h<12:h+=12
        try:d=d.replace(hour=h,minute=minute)
        except ValueError:return None
    return d.isoformat()

def clean(value):
    if value is None:return ''
    if isinstance(value,float) and value.is_integer():return str(int(value))
    return str(value).strip().replace('\u00a0',' ')

def header_key(value):
    text=unicodedata.normalize('NFKC',clean(value)).lower()
    text=re.sub(r'[（(【\[].*?[）)】\]]','',text)
    return re.sub(r'[\s\W_]+','',text)

HEADER_ALIASES={
    'job':{
        'updated':['更新时间','更新日期','信息更新时间','发布日期'],
        'category':['类型','单位类型','企业类型','单位性质','企业性质'],
        'company':['单位名称','企业名称','公司名称','招聘单位','用人单位'],
        'start':['开始时间','开始日期','网申开始时间','投递开始时间'],
        'deadline':['截止时间','截止日期','网申截止时间','投递截止日期','申请截止日期'],
        'method':['网申方式','投递方式','申请方式','报名方式'],
        'application':['网申链接','投递链接','申请链接','报名链接','简历投递'],
        'positions':['招聘岗位','招聘职位','招聘岗位信息','岗位','职位'],
        'location':['工作地点','工作城市','岗位地点','招聘地点'],
        'education':['学历要求','学历','招聘学历'],
        'salary':['薪资情况','薪资待遇','薪酬情况','薪酬待遇','薪资'],
        'announcement':['招聘公告链接','公告链接','招聘公告','招聘信息链接','详情链接'],
        'notes':['备注','补充信息','其他说明'],
    },
    'event':{
        'updated':['更新时间','更新日期','信息更新时间','发布日期'],
        'time':['宣讲时间','宣讲会时间','活动时间','活动日期及时间','日期时间','时间'],
        'location':['宣讲地点','宣讲会地点','活动地点','举办地点','会场','地点'],
        'company':['参会企业','宣讲企业','企业名称','单位名称','公司名称','宣讲会名称'],
        'notes':['备注','补充信息','活动说明','报名信息','说明'],
    },
}

def detect_header(rows,kind):
    aliases={field:[header_key(x) for x in names] for field,names in HEADER_ALIASES[kind].items()}
    required={'company'} if kind=='job' else {'company','time'}
    best=None
    for rownum,cols in sorted(rows.items()):
        if rownum>40:break
        matches={}
        for col,value in cols.items():
            key=header_key(value)
            if not key:continue
            candidates=[]
            for field,names in aliases.items():
                for alias in names:
                    if key==alias:score=30+len(alias)
                    elif len(alias)>=2 and (key.startswith(alias) or key.endswith(alias)):score=20+len(alias)
                    elif len(alias)>=3 and alias in key:score=10+len(alias)
                    else:continue
                    candidates.append((score,field))
            if candidates:
                score,field=max(candidates)
                if field not in matches or score>matches[field][0]:matches[field]=(score,col)
        columns={field:col for field,(_,col) in matches.items()}
        minimum=4 if kind=='job' else 3
        if required<=columns.keys() and len(columns)>=minimum:
            rank=(len(columns),sum(score for score,_ in matches.values()),-rownum)
            if best is None or rank>best[0]:best=(rank,rownum,columns)
    if best:return best[1],best[2]
    observed=[]
    for _,cols in sorted(rows.items())[:5]:
        values=[clean(v) for _,v in sorted(cols.items()) if clean(v)]
        if values:observed=values;break
    label='招聘' if kind=='job' else '宣讲会'
    detail='、'.join(observed[:8]) or '工作表为空'
    raise ValueError(f'{label}信息未找到可识别表头（当前内容：{detail}）')

def safe_link(value):
    value=clean(value)
    if value.lower().startswith(('https://','http://','mailto:')):return value
    email=re.search(r'[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',value)
    if email:return 'mailto:'+email[0]
    if re.match(r'^(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/|$)',value):return 'https://'+value
    return ''

def objects(doc):return {o['id']:o for v in doc.get('versions',[]) for o in v.get('newObjs',[])}

def extract(doc, sheet):
    objs=objects(doc)
    target=None
    for obj in objs.values():
        if obj.get('clsname')=='et.KObjSheet':
            data=obj['data']; inner=objs[data['innerData']]['data']; name=objs[inner['atomData']]['data']['name']
            if name==sheet and data.get('gridCells') in objs:target=obj;break
    if not target:raise ValueError('工作表未能完整加载：'+sheet)
    rows={};grid=objs[target['data']['gridCells']]['data']
    for block in grid['blocks']:
        for row in block['blockRows']:
            rid=block['topRow']+row['blockRowIndex'];dest=rows.setdefault(rid,{})
            for col,cell in enumerate(row['cells'],block['leftCol']+row.get('prevStep',0)):
                data=cell.get('value',{}).get('data',{})
                val=data.get('str',data.get('val',''))
                if not val and 'richText' in data: val=''.join(x.get('text','') for x in data['richText'])
                dest[col]=val
    links={o['id']:o.get('data',{}).get('address','') for o in doc.get('newCustomObjs',[])}
    hyperlinks={}
    for group in doc.get('hyperlinks',[]):
        if group['objSheet']==target['id']:
            for h in group['hyperlinksBlock']:hyperlinks[(h['row'],h['col'])]=links.get(h['HyperlinkAtom'],'')
    return rows,hyperlinks

def parse_sheet(doc,sheet,year):
    rows,links=extract(doc,sheet);result=[];seen={}
    kind='job' if sheet=='招聘信息' else 'event'
    header_row,columns=detect_header(rows,kind);start=header_row+1
    for rownum,cols in sorted(rows.items()):
        if rownum<start:continue
        if not any(clean(x) for x in cols.values()):continue
        value=lambda field:cols.get(columns.get(field,-1),'')
        if sheet=='招聘信息':
            if not any(clean(value(k)) for k in ['company','application','positions','announcement']):continue
            keys=['updated','category','company','start','deadline','method','application','positions','location','education','salary','announcement','notes']
            item={k:clean(value(k)) for k in keys}
            item['kind']='job';item['company']=item['company'] or '单位名称待补充'
            item['updated_date']=normalize_date(value('updated'),year);item['start_date']=normalize_date(value('start'),year);item['deadline_date']=normalize_date(value('deadline'),year)
            for field,key in [('application','apply_url'),('announcement','announcement_url')]:
                col=columns.get(field,-1);item[key]=safe_link(links.get((rownum,col)) or value(field))
            if item['application'].startswith('=DISPIMG'):item['application']='原表提供二维码，请打开原表查看'
            identity=item['company']+'|'+(item['apply_url'] or item['announcement_url'])
        else:
            if not clean(value('company')):continue
            raw_time=value('time');when=normalize_date(raw_time,year,True)
            numeric=isinstance(raw_time,(int,float)) and raw_time>20000
            has_time=(numeric and abs(raw_time-round(raw_time))>1e-8) or bool(re.search(r'\d[:：]\s*\d',str(raw_time)))
            display=dt.datetime.fromisoformat(when).strftime('%Y-%m-%d %H:%M' if has_time else '%Y-%m-%d 时间待定') if numeric and when else clean(raw_time)
            base_cols=set(columns.values());note_values=[]
            if clean(value('notes')):note_values.append(clean(value('notes')))
            note_values.extend(clean(v) for c,v in sorted(cols.items()) if c not in base_cols and clean(v))
            notes='\n'.join(dict.fromkeys(x for x in note_values if not x.startswith('=DISPIMG')))
            item={'kind':'event','company':clean(value('company')),'time_text':display,'starts_at':when,'date':when[:10] if when else None,'time_known':bool(has_time),'location':clean(value('location')),'updated':clean(value('updated')),'updated_date':normalize_date(value('updated'),year),'notes':notes,'apply_url':safe_link(re.search(r'https?://[^\s]+',notes)[0]) if re.search(r'https?://[^\s]+',notes) else ''}
            times=re.findall(r'(\d{1,2})\s*[:：]\s*(\d{2})',display)
            item['ends_at']=None
            if len(times)>1 and when:
                try:item['ends_at']=dt.datetime.fromisoformat(when).replace(hour=int(times[1][0]),minute=int(times[1][1])).isoformat()
                except ValueError:pass
            identity=item['company']+'|'+(item['starts_at'] or item['time_text'])+'|'+item['location']
        digest=hashlib.sha256((item['kind']+'|'+identity).encode()).hexdigest()[:24];seen[digest]=seen.get(digest,0)+1
        item.update(id=digest+('-'+str(seen[digest]) if seen[digest]>1 else ''),source='kdocs',source_row=rownum+1,source_sheet=sheet,hidden=False,pinned=False)
        result.append(item)
    if not result:raise ValueError('工作表没有可用记录，保留上次数据')
    return result

def fetch_source(url=SOURCE):
    if not re.fullmatch(r'https://www\.kdocs\.cn/l/[A-Za-z0-9]+',url):raise ValueError('仅支持金山文档标准分享链接')
    records=[];title='';version=None
    for sheet in ['招聘信息','宣讲会信息']:
        op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        headers={'Cookie':'hadSingleSign=true','User-Agent':'Mozilla/5.0','Referer':url}
        html=op.open(urllib.request.Request(url,headers=headers),timeout=30).read(4_000_000).decode()
        if 'window.__WPSENV__=' not in html:raise ValueError('源表不可公开读取，请检查分享权限')
        env,_=json.JSONDecoder().raw_decode(html.split('window.__WPSENV__=',1)[1]);info=env.get('file_info',{}).get('file',{})
        if not info:raise ValueError('源表不存在或分享权限已变更')
        if version is not None and version!=info.get('version'):raise ValueError('读取期间原表已更新，将在下次同步重试')
        version=info.get('version');title=info['name'].removesuffix('.xlsx');year=int(re.search(r'20\d{2}',title)[0]) if re.search(r'20\d{2}',title) else dt.datetime.now(TZ).year
        payload={'connid':env['conn_id'],'args':{'readonly':True},'ex_args':{'queryInitArgs':{'activeSheet':sheet,'cellLeftTop':{'row':0,'col':0}}},'group':env['user_group'],'front_ver':env['file_version']}
        headers.update({'Content-Type':'application/json','x-csrf-rand':env['csrf_token'],**env.get('request_header_inject',{})})
        endpoint='https://www.kdocs.cn/api/v3/office/file/'+url.rsplit('/',1)[1]+'/open/et'
        binary=op.open(urllib.request.Request(endpoint,data=json.dumps(payload).encode(),headers=headers),timeout=40).read(16_000_000)
        records+=parse_sheet(BinVar(binary).read(),sheet,year)
    return records,{'title':title,'version':version,'url':url}
