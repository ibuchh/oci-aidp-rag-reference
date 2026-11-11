#!/usr/bin/env python3
"""Generate editable draw.io pages using embedded vectors from the OCI library."""
from pathlib import Path
import html
import json
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
ICONS = json.loads((HERE / 'assets/oci-icons.json').read_text())['icons']
OUT = HERE / 'vllm-aidp-completed-architecture.drawio'
RED = '#C74634'
INK = '#312D2A'
MUTED = '#66615B'
BLUE = '#276879'
PURPLE = '#765B91'
GREEN = '#477153'
GRAY = '#837B71'


class Page:
    def __init__(self, file, name, key, width, height):
        self.width, self.height, self.n = width, height, 0
        diagram = ET.SubElement(file, 'diagram', id=key, name=name)
        model = ET.SubElement(diagram, 'mxGraphModel', dx='1800', dy='1100', grid='1',
                              gridSize='10', guides='1', tooltips='1', connect='1', arrows='1',
                              fold='1', page='1', pageScale='1', pageWidth=str(width),
                              pageHeight=str(height), math='0', shadow='0', background='#FFFFFF')
        self.root = ET.SubElement(model, 'root')
        ET.SubElement(self.root, 'mxCell', id='0')
        ET.SubElement(self.root, 'mxCell', id='1', parent='0')
        self.nodes = {}

    def uid(self, prefix='c'):
        self.n += 1
        return f'{prefix}-{self.n}'

    def box(self, key, x, y, w, h, value='', fill='#FFFFFF', stroke='#D9D1C8',
            radius=1, dashed=False, extra='', parent='1'):
        style = (f'rounded={radius};arcSize=7;whiteSpace=wrap;html=1;fillColor={fill};'
                 f'strokeColor={stroke};strokeWidth=1.5;fontFamily=Arial;fontColor={INK};'
                 f'fontSize=18;spacing=12;align=left;verticalAlign=top;'
                 f'dashed={int(dashed)};dashPattern=6 4;{extra}')
        c = ET.SubElement(self.root, 'mxCell', id=key, value=value, style=style,
                          vertex='1', parent=parent)
        ET.SubElement(c, 'mxGeometry', x=str(x), y=str(y), width=str(w), height=str(h), **{'as':'geometry'})
        self.nodes[key] = (x, y, w, h)
        return key

    def text(self, key, x, y, w, h, value, size=19, color=INK, bold=False, align='left', extra=''):
        return self.box(key,x,y,w,h,value,'none','none',0,extra=(
            f'fontSize={size};fontColor={color};fontStyle={int(bold)};align={align};'
            f'spacing=0;verticalAlign=middle;{extra}'))

    def icon(self, name, x, y, size=52):
        icon = ICONS[name]
        scale = min(size / icon['width'], size / icon['height'])
        dx = x + (size-icon['width']*scale)/2
        dy = y + (size-icon['height']*scale)/2
        group = self.uid('oci-'+name)
        self.box(group,x,y,size,size,'','none','none',0,extra='connectable=0;')
        for p in icon['parts']:
            c = ET.SubElement(self.root,'mxCell',id=self.uid('vector'),value='',
                              style=p['style']+';resizable=0;rotatable=0;',vertex='1',
                              connectable='0',parent=group)
            ET.SubElement(c,'mxGeometry',x=str(dx-x+p['x']*scale),y=str(dy-y+p['y']*scale),
                          width=str(p['w']*scale),height=str(p['h']*scale),**{'as':'geometry'})
        return group

    def label(self, title, detail='', title_size=23, detail_size=16, color=INK):
        return (f'<div style="font-size:{title_size}px;font-weight:bold;color:{color};line-height:1.2">'
                f'{title}</div>' + (f'<div style="font-size:{detail_size}px;color:{MUTED};'
                                   f'line-height:1.3;margin-top:6px">{detail}</div>' if detail else ''))

    def card(self,key,x,y,w,h,title,detail='',icon=None,color=INK,fill='#FFFFFF',stroke='#D9D1C8',title_size=23):
        self.box(key,x,y,w,h,'',fill,stroke)
        offset=22
        if icon:
            self.icon(icon,x+20,y+22,52)
            offset=90
        self.text(key+'-label',x+offset,y+15,w-offset-18,h-28,
                  self.label(title,detail,title_size=title_size,color=color),extra='verticalAlign=top;')
        return key

    def boundary(self,key,x,y,w,h,title,detail,icon='subnet',fill='#FCFAF7',color=GRAY):
        self.box(key,x,y,w,h,'',fill,color,0,True)
        self.icon(icon,x+18,y+15,35)
        self.text(key+'-title',x+65,y+12,w-90,32,title,20,color,True)
        if detail:
            self.text(key+'-detail',x+65,y+44,w-90,28,detail,16,MUTED)

    def port(self, key, side, coordinate):
        """Anchor two facing ports to the same exact canvas coordinate."""
        x,y,w,h = self.nodes[key]
        if side in ('left', 'right'):
            result = (int(side == 'right'), (coordinate-y)/h)
        else:
            result = ((coordinate-x)/w, int(side == 'bottom'))
        assert all(0 <= value <= 1 for value in result), (key, side, coordinate)
        return result

    def edge(self,source,target,points, label='', color=RED, dashed=False,
             start=None,end=None,width=2.5,label_at=None, label_size=17, both=False):
        assert start is not None and end is not None, 'Use explicit aligned ports'
        sx,sy,sw,sh = self.nodes[source]
        tx,ty,tw,th = self.nodes[target]
        route = [(sx+start[0]*sw, sy+start[1]*sh), *points,
                 (tx+end[0]*tw, ty+end[1]*th)]
        assert all(abs(ax-bx) < 1e-6 or abs(ay-by) < 1e-6
                   for (ax,ay),(bx,by) in zip(route, route[1:])), (source,target,route)
        style=(f'edgeStyle=none;rounded=0;html=1;endArrow=block;endFill=1;'
               f'strokeColor={color};strokeWidth={width};fontColor={color};fontFamily=Arial;'
               f'fontSize={label_size};labelBackgroundColor=#FFFFFF;'
               f'dashed={int(dashed)};dashPattern=6 4;jumpStyle=arc;jumpSize=10;'
               f'startArrow={"block" if both else "none"};startFill=1;')
        if start:
            style+=f'exitX={start[0]};exitY={start[1]};exitDx=0;exitDy=0;exitPerimeter=0;'
        if end:
            style+=f'entryX={end[0]};entryY={end[1]};entryDx=0;entryDy=0;entryPerimeter=0;'
        c=ET.SubElement(self.root,'mxCell',id=self.uid('flow'),value='',style=style,
                        edge='1',parent='1',source=source,target=target)
        g=ET.SubElement(c,'mxGeometry',relative='1',**{'as':'geometry'})
        if points:
            arr=ET.SubElement(g,'Array',**{'as':'points'})
            for x,y in points: ET.SubElement(arr,'mxPoint',x=str(x),y=str(y))
        if label:
            if label_at:
                x,y,w,h=label_at
                self.text(self.uid('flow-label'),x,y,w,h,label,label_size,color,align='center',
                          extra='labelBackgroundColor=#FFFFFF;')
            else:
                c.set('value',label)
        return c

    def free_edge(self,x1,y1,x2,y2,label='',color=RED,dashed=False,offset=-22):
        assert x1 == x2 or y1 == y2, 'Sequence arrows must be axis-aligned'
        style=(f'edgeStyle=none;rounded=0;html=1;endArrow=block;endFill=1;'
               f'strokeColor={color};strokeWidth=2.4;dashed={int(dashed)};dashPattern=6 4;')
        c=ET.SubElement(self.root,'mxCell',id=self.uid('message'),value='',style=style,edge='1',parent='1')
        g=ET.SubElement(c,'mxGeometry',relative='1',**{'as':'geometry'})
        ET.SubElement(g,'mxPoint',x=str(x1),y=str(y1),**{'as':'sourcePoint'})
        ET.SubElement(g,'mxPoint',x=str(x2),y=str(y2),**{'as':'targetPoint'})
        if label:
            self.text(self.uid('message-label'),min(x1,x2)+18,y1+offset-18,abs(x2-x1)-36,34,
                      label,19,color,align='center',extra='labelBackgroundColor=#FFFFFF;')


def architecture(file):
    p=Page(file,'01 - Reference architecture','completed-architecture',2200,1780)
    p.text('brand',45,25,1500,35,'ORACLE CLOUD INFRASTRUCTURE',20,RED,True)
    p.text('title',45,70,2050,60,'Private inference with vLLM and Oracle AI Data Platform',38,INK,True)
    p.text('subtitle',45,130,2050,30,'Reference standalone POC • One model across 2 × NVIDIA A10 • Qwen3-8B • Same-tenancy integration',20,MUTED)
    p.box('tenancy',240,190,1910,1540,'','#FFFFFF','#B8AEA1',0)
    p.text('tenancy-title',270,200,1770,35,'CUSTOMER TENANCY   •   POC compartment   •   selected OCI region',21,INK,True)

    p.card('aidp',300,275,800,240,'Oracle AI Data Platform',
           '<b>Private workspace:</b> vllm-aidp-poc-private<br>'
           '<b>Agent:</b> vllm_aidp_poc_agent<br>'
           '<b>AI Compute:</b> 1 OCPU · 16 GB · one replica<br>'
           'Oracle-managed runtime · LangGraph orchestration',icon='ai',stroke=RED)
    p.card('vault',1160,275,420,240,'OCI Vault',
           'Dedicated HSM key<br>Inference API secret<br>Lakehouse ADMIN secret<br>'
           'Named Credential Store reference',icon='vault',title_size=24)
    p.card('iam',1640,275,420,240,'IAM and trust',
           'AIDP service policies<br>Exact instance / secret / key<br>'
           'Trusted private TLS certificate<br>No key embedded in agent source',icon='policies',title_size=24)
    p.text('managed-note',1110,540,950,35,
           'AI Compute is Oracle-managed; its customer VCN attachment is shown below.',16,MUTED)
    p.card('caller',30,305,150,165,'Users / CLI','OCI sign-in<br>or signed API',icon=None,title_size=22,fill='#F7F4EF')
    p.icon('user',76,239,58)

    p.boundary('vcn',270,605,1850,1000,'CUSTOMER VCN · 10.160.0.0/16',
               'Private workload access; only the monitoring web proxy has a public VM endpoint',icon='vcn',fill='#FCFCFA',color=BLUE)
    # Keep header copy outside the vertical managed-runtime connector corridor.
    vcn_detail = next(c for c in p.root if c.get('id') == 'vcn-detail')
    vcn_detail.find('mxGeometry').set('x', '1200')
    vcn_detail.find('mxGeometry').set('y', '640')
    vcn_detail.find('mxGeometry').set('width', '850')
    p.boundary('aidp-subnet',300,690,560,460,'AIDP / Lakehouse private subnet','10.160.36.0/24',fill='#F8FAF8',color=GREEN)
    p.card('attachment',330,790,500,140,'Private network attachment',
           'Workspace subnet + NSG<br>Verified HTTPS egress to private vLLM',icon='nsg',title_size=22)
    p.card('lakehouse',330,990,500,130,'Autonomous AI Lakehouse 26ai',
           'Private endpoint · 2 ECPUs · 1 TB<br>AI-feature foundation; no SQL/RAG in this test',icon='database',title_size=20)
    p.text('foundation-label',350,947,460,26,'Lakehouse association, outside the inference request path',15,GREEN)
    p.card('dns',920,700,220,120,'Private DNS','inference.lab<br>GPU private IP',icon='dns',title_size=19)

    p.boundary('gpu-subnet',1200,690,860,460,'GPU private subnet','10.160.33.0/24',fill='#FCF8F6',color=RED)
    p.card('gpu-vm',1230,790,800,330,'OCI Compute · VM.GPU.A10.2',
           'GPU private IP · Ubuntu 22.04 · no public IP',icon='vm',title_size=23)
    p.card('nginx',1260,880,240,100,'Nginx · TLS :443',
           'OpenAI chat API<br>Bearer authentication',title_size=19,stroke=RED)
    p.card('vllm',1560,880,440,100,'vLLM 0.11.0 · qwen3-8b',
           'OpenAI-compatible API · loopback :8000<br>Qwen3-8B · FP16 · tensor parallelism 2',title_size=21,stroke=RED)
    p.card('gpu0',1560,1010,205,80,'A10 · GPU 0','24 GB VRAM',title_size=18,fill='#F8F4EF')
    p.card('gpu1',1795,1010,205,80,'A10 · GPU 1','24 GB VRAM',title_size=18,fill='#F8F4EF')
    p.text('settings',1260,995,270,65,'4,096-token context<br>4 active sequences · 85% GPU budget<br>Hermes automatic tool calling',15,MUTED)
    p.text('exporters',1260,1070,270,22,'vLLM / GPU / host exporters',14,PURPLE)

    p.boundary('web-subnet',300,1200,800,340,'Monitoring public subnet','10.160.35.0/24 · public HTTPS on TCP 443',fill='#F4F8FA',color=BLUE)
    p.icon('vm',330,1280,40)
    p.text('web-host',385,1275,670,30,'Monitoring VM · monitor private IP · E5 Flex · 2 OCPUs / 16 GB',18,INK,True)
    p.card('caddy',330,1330,200,110,'HTTPS proxy','TLS + login<br>sslip.io / ACME',title_size=19,stroke=BLUE)
    p.card('streamlit',590,1320,220,80,'Streamlit','Chat UI · :8501',title_size=19,stroke=BLUE)
    p.card('grafana',590,1430,220,80,'Grafana','Dashboards · :3000',title_size=19,stroke=PURPLE)
    p.card('prom',870,1430,200,80,'Prometheus','Scrape/store · :9090',title_size=19,stroke=PURPLE)
    p.icon('block',870,1305,30)
    p.text('metrics-volume',915,1305,170,40,'100 GB metrics disk<br>15-day retention cap',14,MUTED)
    p.card('web-user',30,1300,150,140,'Users','Browser<br>HTTPS + login',title_size=21,fill='#F7F4EF')
    p.icon('user',76,1239,52)

    p.boundary('fss-subnet',1200,1200,400,340,'FSS private subnet','10.160.34.0/24',fill='#FBFAF6',color=GRAY)
    p.card('fss',1230,1320,340,160,'OCI File Storage',
           'FSS mount IP:/models<br>Pinned model weights<br>Read-only container mount',icon='fss',title_size=20)
    p.boundary('bastion-subnet',1660,1200,400,340,'Bastion private subnet','10.160.32.0/24',fill='#FBFAF6',color=GRAY)
    p.card('bastion',1690,1320,340,160,'OCI Bastion',
           'Mac operator · allowed /32<br>SSH :22 → GPU / monitor<br>Console-verified host keys',icon='bastion',title_size=20)

    p.edge('caller','aidp',[],color=RED,start=p.port('caller','right',380),end=p.port('aidp','left',380),
           label='1 · Invoke',label_at=(177,350,118,32))
    p.edge('aidp','vault',[],color=BLUE,start=p.port('aidp','right',380),end=p.port('vault','left',380),
           label='Vault<br>reference',label_at=(1105,330,50,40),label_size=12)
    p.edge('aidp','attachment',[],color=RED,start=p.port('aidp','bottom',750),end=p.port('attachment','top',750),
           label='2 · Private workspace access',label_at=(750,572,360,32))
    p.edge('attachment','nginx',[],color=RED,start=p.port('attachment','right',910),end=p.port('nginx','left',910),
           label='3 · Private HTTPS + bearer key',label_at=(845,846,390,28))
    p.text('endpoint-label',855,878,350,25,'inference.lab/v1/chat/completions',16,RED,align='center')
    p.edge('attachment','dns',[(890,820),(890,760)],color=GRAY,dashed=True,start=p.port('attachment','right',820),end=p.port('dns','left',760),
           label='Resolve',label_at=(833,741,88,28),label_size=14)
    p.edge('nginx','vllm',[],color=RED,start=p.port('nginx','right',930),end=p.port('vllm','left',930),width=2.7)
    p.edge('vllm','gpu0',[],color=RED,start=p.port('vllm','bottom',1662.5),end=p.port('gpu0','top',1662.5),width=2)
    p.edge('vllm','gpu1',[],color=RED,start=p.port('vllm','bottom',1897.5),end=p.port('gpu1','top',1897.5),width=2)
    p.edge('web-user','caddy',[],color=BLUE,start=p.port('web-user','right',1360),end=p.port('caddy','left',1360),
           label='HTTPS',label_at=(186,1320,140,28),label_size=16)
    p.edge('caddy','streamlit',[],color=BLUE,start=p.port('caddy','right',1360),end=p.port('streamlit','left',1360),width=2)
    p.edge('caddy','grafana',[(560,1410),(560,1470)],color=PURPLE,start=p.port('caddy','right',1410),end=p.port('grafana','left',1470),width=2)
    p.edge('grafana','prom',[],color=PURPLE,start=p.port('grafana','right',1470),end=p.port('prom','left',1470),width=2)
    p.edge('streamlit','nginx',[(1130,1360),(1130,950)],color=BLUE,start=p.port('streamlit','right',1360),end=p.port('nginx','left',950),
           label='Direct private inference',label_at=(815,1366,325,32),label_size=16)
    # Prometheus uses the GPU's private metrics listener, represented by its VM boundary.
    p.edge('prom','gpu-vm',[(1170,1470),(1170,1170),(1310,1170)],color=PURPLE,dashed=True,
           start=p.port('prom','right',1470),end=p.port('gpu-vm','bottom',1310),
           label='Private metrics scrape',label_at=(870,1395,200,25),label_size=15)
    p.edge('vllm','fss',[(2090,930),(2090,1180),(1630,1180),(1630,1400)],color=GRAY,dashed=True,
           start=p.port('vllm','right',930),end=p.port('fss','right',1400),
           label='NFS read · persistent model weights',label_at=(1700,1155,380,20),label_size=16)

    # Foundation/service association is distinct from the model request path.
    p.text('foundation-note',305,1156,760,28,'Lakehouse and Vault enable AIDP features; the demonstrated tool returns synthetic inventory.',15,GREEN)
    for name,x,title in [('nat',300,'NAT gateway'),('service_gateway',650,'Service gateway'),
                         ('internet_gateway',1020,'Internet gateway'),('nsg',1415,'NSGs + host firewall')]:
        p.icon(name,x,1630,42)
        p.text('infra-'+name,x+55,1633,315,35,title,18,MUTED)
    p.text('egress-note',300,1690,1750,24,'Gateways provide bootstrap / service egress; the GPU API has no public endpoint. Public application listeners are behind the monitoring HTTPS proxy.',15,MUTED)


def sequence(file):
    """Trace one successful request through the native RAG graph."""
    p=Page(file,'02 - AIDP RAG request flow','verified-request-flow',2200,1370)
    p.text('brand',45,25,1800,35,'ORACLE CLOUD INFRASTRUCTURE',20,RED,True)
    p.text('title',45,75,2070,55,'How AIDP RAG produces a cited answer',38,INK,True)
    p.text('subtitle',45,135,2090,35,
           'Question → native knowledge-base retrieval → grounded private generation → answer with source citations',20,MUTED)
    caller, agent, kb, vault, llm = [230 + 430*i for i in range(5)]
    lanes=[('caller','User / Playground','OCI sign-in or signed API',caller,'user'),
           ('agent','AIDP RAG agent','AI Compute · LangGraph',agent,'ai'),
           ('kb','Native AIDP KB','Lakehouse 26ai<br>Embedding + vector search',kb,'database'),
           ('vault','Credential Store + Vault','Named inference credential',vault,'vault'),
           ('llm','Private vLLM + GPUs','Qwen3-8B · 2 × A10',llm,'vm')]
    for key,title,detail,x,icon in lanes:
        p.card(key,x-170,230,340,125,title,detail,icon=icon,title_size=21,
               fill='#F8F5F0' if key!='agent' else '#FCF2EF',stroke=RED if key=='agent' else '#CFC5B9')
        p.free_edge(x,372,x,1320,color='#DDD7D0',dashed=True)
        p.root[-1].set('style',p.root[-1].get('style').replace('endArrow=block','endArrow=none'))
    p.box('agent-active',agent-8,402,16,875,'','#F2DDD7',RED,0)
    p.box('kb-active',kb-8,500,16,130,'','#EEE8F4',PURPLE,0)
    p.box('credential-active',vault-8,830,16,130,'','#E8F1F3',BLUE,0)
    p.box('model-active',llm-8,1030,16,130,'','#F2DDD7',RED,0)
    p.free_edge(caller,420,agent,420,'1 · Ask a question')
    p.free_edge(agent,520,kb,520,'2 · Native vector search',color=PURPLE)
    p.free_edge(kb,620,agent,620,'Top 3 chunks + source filenames',color=PURPLE,dashed=True)
    p.box('grounded-prompt',agent-170,685,340,85,
          p.label('Build grounded prompt','Question + retrieved excerpts<br>Untrusted context + citation rules',title_size=20,detail_size=16),
          fill='#FCF2EF',stroke='#CDA69D',extra='align=center;verticalAlign=middle;spacing=8;')
    p.free_edge(agent,840,vault,840,'Resolve the named inference credential',color=BLUE)
    p.free_edge(vault,950,agent,950,'Key available only in runtime memory',color=BLUE,dashed=True)
    p.free_edge(agent,1040,llm,1040,'3 · Grounded prompt · verified private HTTPS /v1/chat/completions')
    p.free_edge(llm,1150,agent,1150,'4 · Model answer grounded in retrieved excerpts',color=BLUE,dashed=True)
    p.free_edge(agent,1260,caller,1260,'5 · Answer + source citations',color=BLUE,dashed=True)


def rag_architecture(file, name='03 - AIDP RAG architecture'):
    """Logical service view of the native KB and verified private generation path."""
    p=Page(file,name,'aidp-rag-architecture',2400,1160)
    p.text('rag-brand',55,25,1900,32,'ORACLE CLOUD INFRASTRUCTURE',19,RED,True)
    p.text('rag-title',55,72,2250,60,'AIDP RAG with private vLLM',42,INK,True)
    p.text('rag-subtitle',55,143,2250,36,
           'Native knowledge base + Lakehouse vector search + grounded answers on existing GPUs',23,MUTED)
    p.box('rag-rule',55,202,2290,3,'',RED,RED,0)
    p.boundary('rag-aidp',380,235,1320,870,'ORACLE AI DATA PLATFORM',
               'Logical service view · native catalog, ingestion and managed orchestration',icon='ai',fill='#FCFAF7',color=GRAY)
    p.boundary('rag-vcn',1740,235,605,870,'CUSTOMER PRIVATE NETWORK',
               'Associated Lakehouse and private GPU endpoint',icon='vcn',fill='#FCFCFA',color=BLUE)
    p.text('rag-ingest-heading',430,330,1200,34,'A · MANUAL DOCUMENT INGESTION',21,RED,True)
    p.card('rag-docs',55,392,270,185,'Approved documents',
           'Release policy<br>Support &amp; escalation<br>Data handling<br>TXT · PDF · DOCX',title_size=22,fill='#F8F5F0')
    p.card('rag-volume',430,402,330,170,'Managed volume',
           'Dedicated RAG schema<br>synthetic_documents<br>Upload records + file hashes',icon='block',title_size=22)
    p.card('rag-spark',810,402,330,170,'Ingestion compute',
           'Read and split documents<br>Existing Spark compute<br>Default Master Catalog Compute',icon='vm',title_size=21)
    p.card('rag-kb',1190,402,450,170,'Native AIDP knowledge base',
           'aster_knowledge_base<br>1000-character chunks<br>150-character overlap',icon='ai',title_size=22,stroke=RED)
    p.card('rag-vectors',1790,370,505,225,'Associated Lakehouse 26ai',
           'In-database embeddings<br>ALL_MINILM_L12_V2<br>Vector chunks + source metadata<br>Native vector search',icon='database',title_size=23,stroke=GREEN,fill='#F6FAF6')
    p.edge('rag-docs','rag-volume',[],color=GRAY,
           start=p.port('rag-docs','right',487),end=p.port('rag-volume','left',487))
    p.edge('rag-volume','rag-spark',[],color=GRAY,
           start=p.port('rag-volume','right',487),end=p.port('rag-spark','left',487))
    p.edge('rag-spark','rag-kb',[],color=GRAY,
           start=p.port('rag-spark','right',487),end=p.port('rag-kb','left',487))
    p.edge('rag-kb','rag-vectors',[],color=GREEN,
           start=p.port('rag-kb','right',487),end=p.port('rag-vectors','left',487))
    p.text('rag-ingest-note',430,600,1200,54,
           '<b>Explicit gate:</b> create the native KB and source in AIDP, then ingest.<br>'
           'Job runs must show Succeeded. Uploading files alone does not update the searchable index.',19,MUTED)
    p.text('rag-runtime-heading',430,745,710,35,'B · ONLINE RETRIEVAL AND GENERATION',21,RED,True)
    p.card('rag-caller',55,860,270,180,'AIDP Playground / CLI',
           'OCI sign-in<br>or signed agent endpoint<br>Question + cited answer',icon=None,title_size=23,fill='#F8F5F0')
    p.icon('user',162,790,56)
    p.card('rag-agent',530,855,950,210,'Separate RAG agent · LangGraph on AI Compute',
           '<b>Retrieve:</b> native KB metadata + top 3 vector-search chunks<br>'
           '<b>Ground:</b> question + retrieved excerpts + source filenames<br>'
           '<b>Generate:</b> verified HTTPS to private vLLM; return a cited answer<br>'
           'Existing CPU runtime · 1 OCPU / 16 GB · private subnet + NSG attachment',
           icon='ai',title_size=26,stroke=RED,fill='#FDF3F0')
    p.card('rag-vault',1790,635,505,130,'OCI Vault inference secret',
           'Resolved through AIDP Credential Store<br>Key available only in runtime memory',icon='vault',title_size=22)
    p.card('rag-gpu',1790,855,505,210,'Private vLLM · Qwen3-8B',
           'Nginx HTTPS + bearer authentication<br>inference.lab/v1/chat/completions<br>'
           'VM.GPU.A10.2 · 2 × NVIDIA A10<br>No public model endpoint',icon='vm',title_size=24,stroke=RED)
    p.edge('rag-caller','rag-agent',[],color=RED,
           start=p.port('rag-caller','right',930),end=p.port('rag-agent','left',930))
    p.text('rag-question-label',335,888,185,32,'1 · Ask',20,RED,align='center',extra='fillColor=#FFFFFF;spacing=3;')
    vector_flow=p.edge('rag-agent','rag-vectors',[(1250,730),(1660,730),(1660,550)],
           color=PURPLE,both=True,start=p.port('rag-agent','top',1250),
           end=p.port('rag-vectors','left',550))
    vector_flow.set('style',vector_flow.get('style').replace('jumpStyle=arc','jumpStyle=none'))
    p.text('rag-retrieve-label',1260,660,380,60,
           '<b>2 · Native vector search</b><br>'
           '<span style="font-size:16px">Top 3 chunks + source filenames</span>',20,PURPLE,align='center')
    p.edge('rag-agent','rag-vault',[(1420,790),(2042,790)],color=GRAY,dashed=True,both=True,
           start=p.port('rag-agent','top',1420),end=p.port('rag-vault','bottom',2042))
    p.text('rag-key-label',1400,750,300,30,'Named credential reference',18,GRAY,align='center')
    p.edge('rag-agent','rag-gpu',[],color=RED,
           start=p.port('rag-agent','right',930),end=p.port('rag-gpu','left',930))
    p.text('rag-prompt-label',1495,888,275,32,'3 · Grounded prompt',20,RED,align='center',extra='fillColor=#FFFFFF;spacing=3;')
    p.edge('rag-gpu','rag-agent',[],color=BLUE,dashed=True,
           start=p.port('rag-gpu','left',1010),end=p.port('rag-agent','right',1010))
    p.text('rag-model-answer-label',1495,968,275,32,'4 · Model answer',20,BLUE,align='center',extra='fillColor=#FFFFFF;spacing=3;')
    p.edge('rag-agent','rag-caller',[],color=BLUE,dashed=True,
           start=p.port('rag-agent','left',1010),end=p.port('rag-caller','right',1010))
    p.text('rag-answer-label',335,944,185,56,
           '<b>5 · Cited answer</b><br>'
           '<span style="font-size:14px">[source-filename.txt]</span>',20,BLUE,
           align='center',extra='fillColor=#FFFFFF;spacing=3;')


def main():
    file=ET.Element('mxfile',host='Electron',agent='Customer OCI POC architecture generator',
                    version='29.0.0',type='device',compressed='false')
    architecture(file)
    sequence(file)
    rag_architecture(file)
    ET.indent(file,space='  ')
    OUT.write_bytes(ET.tostring(file,encoding='utf-8',xml_declaration=True))
    # Structural checks protect editability without calling OCI or touching state.
    for d in file.findall('diagram'):
        cells=d.findall('.//mxCell')
        ids=[c.get('id') for c in cells]
        assert len(ids)==len(set(ids)), 'Duplicate cell IDs'
        for c in cells:
            for attr in ('source','target','parent'):
                assert c.get(attr) is None or c.get(attr) in ids, (c.get('id'),attr)
        print(d.get('name'),':',len(cells),'editable cells; references valid')
    print(OUT)


if __name__=='__main__':
    main()
