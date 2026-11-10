"""Render reviewed dated series from JSON: python plot_history.py INPUT.json OUTPUT.png.

Requires Pillow. Input: title, subtitle, series [{id,title,unit,points:[
{date,value,source_kind}]}], panels [{series,ymax,step,title?}].
For layout=comparison: panels contain series IDs, ymin/ymax/step and optional
ylabels/connect/max_gap_days/note. date_range sets the common time axis.
An optional third CLI argument overrides its start date for a detailed view.
Medical values and source selection belong in the reviewed input, not this script.
"""
import json
import os
import sys
from datetime import date
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


def chart_font(size):
    """Resolve a Cyrillic-capable font without a mandatory Windows installation."""
    explicit = os.environ.get('HEALTH_FONT')
    candidates = [explicit] if explicit else [
        'DejaVuSans.ttf', 'Arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/System/Library/Fonts/Supplemental/Arial.ttf',
    ]
    if not explicit and os.environ.get('WINDIR'):
        candidates.append(str(Path(os.environ['WINDIR']) / 'Fonts' / 'arial.ttf'))
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    raise OSError('Install a Cyrillic font or set HEALTH_FONT to its .ttf path')


def render_comparison(data, output, start_override=None):
    """Shared time axis, multiple series per panel, categorical event lanes.

    Optional point label/uncertain; panel ymin/ymax/step/max_gap_days;
    event panels use categorical ylabels. No intervals inferred from event dates.
    """
    scale = 2
    width, height = 1650, 315 + 345 * len(data['panels'])
    canvas = Image.new('RGB', (width*scale, height*scale), '#f7f9fc')
    draw = ImageDraw.Draw(canvas)
    def txt(x,y,value,size=20,fill='#26354b',anchor=None):
        font=chart_font(size*scale)
        draw.text((int(x*scale),int(y*scale)),str(value),font=font,fill=fill,anchor=anchor)
    def ln(coords,color='#dce3ed',stroke=1):
        draw.line([(int(x*scale),int(y*scale)) for x,y in coords],fill=color,width=stroke*scale)
    start=date.fromisoformat(start_override or data['date_range'][0])
    end=date.fromisoformat(data['date_range'][1])
    left,right=245,1550
    x=lambda d:left+(date.fromisoformat(d)-start).days/(end-start).days*(right-left)
    txt(80,30,data['title'],34)
    txt(80,80,data['subtitle'],20)
    txt(80,112,f"Период: {start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}",18,'#617085')
    series={s['id']:s for s in data['series']}
    for n,panel in enumerate(data['panels']):
        top=230+n*345; bottom=top+220
        ymin,ymax=panel['ymin'],panel['ymax']
        y=lambda v:bottom-(v-ymin)/(ymax-ymin)*(bottom-top)
        txt(80,top-72,panel['title'],25)
        if panel.get('note'):txt(80,top-40,panel['note'],17,'#617085')
        if 'ylabels' in panel:
            ticks=[(float(k),v) for k,v in panel['ylabels'].items()]
        else:
            ticks=[(ymin+i*panel['step'],f"{ymin+i*panel['step']:g}".replace('.',',')) for i in range(round((ymax-ymin)/panel['step'])+1)]
        for v,label in ticks:
            ln([(left,y(v)),(right,y(v))]);txt(left-16,y(v),label,18,anchor='rm')
        span=(end-start).days
        # Calendar ticks shared by all panels; short views include half-years.
        ticks=[]
        for year in range(start.year,end.year+1):
            for month in ([1,7] if span<2500 else [1]):
                day=date(year,month,1)
                if start<=day<=end and (span<5000 or year%2==0):ticks.append(day)
        for d in ticks:
            xpos=x(d.isoformat());ln([(xpos,top),(xpos,bottom)],'#e3e9f1')
            label=str(d.year) if d.month==1 else 'июл'
            txt(xpos,bottom+12,label,17,anchor='mt')
        for i,sid in enumerate(panel['series']):
            s=series[sid];color=s['color']
            if panel.get('legend',True):
                xx=left+i*245; ln([(xx,top-14),(xx+24,top-14)],color,3)
                txt(xx+32,top-14,s['title'],18,color,anchor='lm')
            ps=sorted([p for p in s['points'] if start.isoformat()<=p['date']<=end.isoformat()],key=lambda p:p['date'])
            if panel.get('connect',True):
                for a,b in zip(ps,ps[1:]):
                    gap=(date.fromisoformat(b['date'])-date.fromisoformat(a['date'])).days
                    if gap<=panel.get('max_gap_days',730):ln([(x(a['date']),y(a['value'])),(x(b['date']),y(b['value']))],color,2)
            for p in ps:
                px,py=x(p['date']),y(p['value']);r=panel.get('radius',5)
                box=tuple(int(v*scale) for v in (px-r,py-r,px+r,py+r))
                hollow=p['source_kind'] in ['workbook','document_date']
                fill='#f7f9fc' if hollow else color
                if p['source_kind']=='prescription':
                    draw.polygon([(int(px*scale),int((py-r-2)*scale)),(int((px-r-2)*scale),int((py+r)*scale)),(int((px+r+2)*scale),int((py+r)*scale))],fill=color)
                elif p['source_kind']=='workbook':draw.rectangle(box,fill=fill,outline=color,width=2*scale)
                else:draw.ellipse(box,fill=fill,outline=color,width=2*scale)
                if p.get('uncertain'):
                    txt(px,py+22,'?',19,'#bd3743',anchor='mt')
                if p.get('label') and not ('ylabels' in panel and span>5000):
                    below='ylabels' in panel and p['source_kind']=='workbook'
                    txt(px,py+16 if below else py-14,p['label'],17,color,anchor='mt' if below else 'mb')
            if panel.get('label_last') and ps:
                p=ps[-1];txt(right,y(p['value'])-20,f"{p['value']:g}".replace('.',',')+' · '+date.fromisoformat(p['date']).strftime('%d.%m.%Y'),18,color,anchor='rb')
    for i,note in enumerate(data.get('chart_notes',[])):
        txt(80,185+345*len(data['panels'])+i*30,note,18,'#617085')
    output=Path(output)
    if output.exists():raise FileExistsError(output)
    canvas.save(output)


def render(data, output):
    scale = 2
    width, height = 1600, 210 + 350 * len(data['panels'])
    image = Image.new('RGB', (width * scale, height * scale), '#f7f9fc')
    draw = ImageDraw.Draw(image)
    def text(x, y, s, size=22, color='#24344b', anchor=None):
        font = chart_font(size * scale)
        draw.text((x * scale, y * scale), str(s), font=font, fill=color, anchor=anchor)
    def line(coords, color, stroke=1):
        draw.line([(int(x*scale), int(y*scale)) for x,y in coords], fill=color, width=stroke*scale)
    def marker(x,y,color,kind):
        r = 6
        box = tuple(int(v*scale) for v in (x-r,y-r,x+r,y+r))
        if kind == 'workbook':
            draw.rectangle(box, fill='#ffffff', outline='#c57715', width=2*scale)
        elif kind == 'document_date':
            draw.ellipse(box, fill='#ffffff', outline=color, width=2*scale)
        else:
            draw.ellipse(box, fill=color, outline='#ffffff', width=scale)
    text(85, 35, data['title'], 34)
    text(85, 85, data['subtitle'], 20, '#58677b')
    series = {s['id']:s for s in data['series']}
    points = [p for s in series.values() for p in s['points']]
    first = min(date.fromisoformat(p['date']) for p in points)
    last = max(date.fromisoformat(p['date']) for p in points)
    start, end = date(first.year, 1, 1), date(last.year+1,1,1)
    left, right = 100, 1510
    def xpos(d):
        return left + (date.fromisoformat(d)-start).days/(end-start).days*(right-left)
    for n,panel in enumerate(data['panels']):
        s=series[panel['series']]
        color = s.get('color', '#2466ad')
        top, bottom = 180+n*350, 420+n*350
        ymax=panel['ymax']
        def ypos(v): return bottom-v/ymax*(bottom-top)
        text(left, top-46, panel.get('title',s['title'])+' · '+s['unit'], 25)
        for i in range(int(ymax/panel['step'])+1):
            value=i*panel['step']; y=ypos(value)
            line([(left,y),(right,y)],'#dce3ed')
            text(left-18,y,str(value).replace('.',','),18,anchor='rm')
        for year in range(start.year,end.year+1,2):
            x=xpos(f'{year}-01-01')
            line([(x,top),(x,bottom)],'#e7ecf2')
            text(x,bottom+14,year,18,anchor='mt')
        ps=sorted(s['points'],key=lambda p:p['date'])
        # Only connect consecutive visible observations; never bridge an off-scale point.
        for a,b in zip(ps,ps[1:]):
            if a['value']<=ymax and b['value']<=ymax:
                line([(xpos(a['date']),ypos(a['value'])),(xpos(b['date']),ypos(b['value']))], '#b5c4d6',2)
        for p in ps:
            x=xpos(p['date']); v=p['value']
            if v>ymax:
                text(x,top+10,'↑ '+str(v).replace('.',','),18,color,anchor='mt')
            else:
                marker(x,ypos(v),color,p['source_kind'])
        peak=max(ps,key=lambda p:p['value'])
        if peak['value']<=ymax:
            text(xpos(peak['date'])-12,ypos(peak['value'])-12,
                 f"{peak['value']:g}".replace('.',',')+' · '+date.fromisoformat(peak['date']).strftime('%d.%m.%Y'),18,color,anchor='rb')
        p=ps[-1]
        if p['value']<=ymax:
            text(right,ypos(p['value'])-22,f"{p['value']:g}".replace('.',',')+' · '+date.fromisoformat(p['date']).strftime('%d.%m.%Y'),18,color,anchor='rb')
    y=height-85
    marker(105,y+12,'#2466ad','sample_date');text(125,y,'Документ: дата анализа',18)
    marker(490,y+12,'#2466ad','document_date');text(510,y,'Документ: дата бланка, взятие неизвестно',18)
    marker(1135,y+12,'#c57715','workbook');text(1155,y,'Только ручная книга',18)
    text(85,height-40,data.get('footnote','Линии соединяют наблюдения; между ними измерений нет.'),18,'#58677b')
    output=Path(output)
    if output.exists(): raise FileExistsError(output)
    image.save(output)


if __name__ == '__main__':
    data=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    if data.get('layout')=='comparison':
        render_comparison(data,sys.argv[2],sys.argv[3] if len(sys.argv)>3 else None)
    else:
        render(data,sys.argv[2])
