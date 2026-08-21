#!/usr/bin/env python3
"""
build_briefje.py - assemble a branded routebriefje from the generator outputs.

Fills the A4 template by reusing its frame/CSS and injecting:
  - timeline  <- knooppunten strip            (mechanical, trusted)
  - header    <- day/title/distance/ship/QR   (mechanical, trusted)
  - route     <- routes-from-GPX  -> DRAFT scaffold to rewrite  (you edit)
  - Onderweg  <- POIs by type      -> SUGGESTED shortlist to trim (you edit)

The route prose and POI cards are drafts on purpose (hand-authored middle,
no unverified content). LEAVE-NETWORK gaps are marked [VUL IN].

    python build_briefje.py --template Poseidon_..NL..html --notes 213.txt \
        --out brief.html --day "DAG 05" --title "Dordrecht -> Schoonhoven" \
        --sub "(via Biesbosch)" --dist "48 km" --ship Poseidon --phone "+31 6 82824980" \
        --pill-class red --pill-text "Rood - lange route" --qr-code TM6LG \
        --start "Dordrecht, kade" --arrival "Schoonhoven, haven" --lang nl
"""
import argparse, html, re, sys, base64, io

LABELS={
 "nl":{"route":"Route van vandaag","onderweg":"Onderweg","lunch":"Koffie & lunch",
       "high":"Hoogtepunten","safe":"Let op","know":"Goed om te weten",
       "start":"START","arrival":"AANKOMST","fill":"[VUL IN: geschreven route]","callship":"Problemen onderweg? <b>Bel je schip: {s} &nbsp;&middot;&nbsp; {p}</b>","board":"neem de pont","todo":"&lsaquo;nog in te vullen&rsaquo;"},
 "en":{"route":"Today's route","onderweg":"Along the way","lunch":"Coffee & lunch",
       "high":"Highlights","safe":"Take care","know":"Good to know",
       "start":"START","arrival":"ARRIVAL","fill":"[FILL IN: written directions]","callship":"Problems en route? <b>Call your ship: {s} &nbsp;&middot;&nbsp; {p}</b>","board":"board the ferry","todo":"&lsaquo;to be written&rsaquo;"},
 "de":{"route":"Route von heute","onderweg":"Unterwegs","lunch":"Kaffee & Mittag",
       "high":"Höhepunkte","safe":"Achtung","know":"Gut zu wissen",
       "start":"START","arrival":"ANKUNFT","fill":"[EINFÜGEN: schriftliche Route]","callship":"Probleme unterwegs? <b>Ruf dein Schiff an: {s} &nbsp;&middot;&nbsp; {p}</b>","board":"mit der Fähre","todo":"&lsaquo;noch auszufüllen&rsaquo;"},
}
# POI type -> card (default; tune to taste)
CARD={"Coffee":"lunch","Food":"lunch","Bar":"lunch","Rest Stop":"know","Swimming":"know",
      "Viewpoint":"high","Information":"know","ATM":"know","Bike Parking":"know",
      "Ferry":"safe","Caution":"safe"}
SKIP={"Lodging","Start","Finish"}   # docking/mooring/start-finish noise
CAP={"lunch":4,"high":4,"safe":4,"know":3}

def esc(s): return html.escape(str(s),quote=True)

def parse_notes(txt):
    pois=[]; knp=[]; gaps=[]
    for m in re.finditer(r"^\s*([\d.]+) km\s+(.+?)\s+\[([^\]]+)\]\s+(\d+) m\s*$",txt,re.M):
        pois.append({"km":float(m.group(1)),"name":m.group(2).strip(),"type":m.group(3).strip(),"off":int(m.group(4))})
    for m in re.finditer(r"^\s*(\w+)\s+@\s+([\d.]+) km",txt,re.M):
        knp.append({"kp":m.group(1),"km":float(m.group(2))})
    for m in re.finditer(r"LEAVE NETWORK.*?rejoin at (\w+|FINISH)",txt):
        gaps.append(m.group(1))
    return pois,knp,gaps

def timeline_html(knp,start_town,end_town):
    if not knp: return '<div class="timeline"><div class="tl-track" data-rows="4"></div></div>'
    out=['<div class="timeline">','<div class="tl-track" data-rows="4">']
    n=len(knp)
    for i,k in enumerate(knp):
        town=""
        if i==0 and start_town: town=f'<span class="town">{esc(start_town)}</span>'
        if i==n-1: 
            town=f'<span class="town">{esc(end_town)}</span>' if end_town else town
            out.append(f'<div class="node"><span class="dot end">{esc(k["kp"])}</span><span class="km">{k["km"]:.1f}</span>{town}</div>')
        else:
            out.append(f'<div class="node"><span class="dot">{esc(k["kp"])}</span><span class="km">{k["km"]:.1f}</span>{town}</div>')
    out.append('</div></div>')
    return "\n".join(out)

def route_draft(knp,gaps,L):
    """Scaffold: kp runs split at gaps, each a <p> to rewrite; gaps flagged."""
    if not knp: return '<div class="route"><p>'+L["fill"]+'</p></div>'
    ps=['<div class="route">',
        '<p style="background:#FFF6E9;border-left:3px solid #EF9F27;padding:6px 10px">'
        '<b>CONCEPT</b> &mdash; herschrijf tot lopende tekst; de knooppunten kloppen.</p>']
    gapset=set(gaps)
    run=[]
    for k in knp:
        run.append(k)
        if k["kp"] in gapset:
            ps.append(_run_p(run,L)); run=[]
            ps.append(f'<p><span class="jct">gap</span> {L["fill"]}</p>')
    if run: ps.append(_run_p(run,L))
    ps.append('</div>')
    return "\n".join(ps)
def _run_p(run,L):
    kps=" &middot; ".join(f'kp {r["kp"]}' for r in run)
    a,b=run[0]["km"],run[-1]["km"]
    return f'<p>Volg <span class="jct">{kps}</span> ({a:.1f}&ndash;{b:.1f} km). {L["fill"]}</p>'

def poi_cards(pois,L):
    seen=set(); buckets={"lunch":[],"high":[],"safe":[],"know":[]}
    for p in sorted(pois,key=lambda x:x["km"]):
        t=p["type"]
        if t in SKIP: continue
        card=CARD.get(t)
        if not card: continue
        key=re.sub(r"\s+"," ",p["name"].lower()).strip()
        if key in seen or len(p["name"])<3: continue
        seen.add(key)
        if len(buckets[card])>=CAP[card]: continue
        label=p["name"]
        if t=="Ferry": label=f'{p["name"]} &mdash; {L["board"]}'
        buckets[card].append(f'<li>{esc(label)} <span style="color:#7a8394">(~{p["km"]:.0f} km)</span></li>')
    return buckets

def replace_block(hml,open_tag_re,new_html,end_before):
    pat=re.compile(open_tag_re+r".*?</div>\s*(?="+end_before+")",re.S)
    return pat.sub(lambda m:new_html+"\n",hml,count=1)

def qr_datauri(code):
    try:
        import qrcode
    except ImportError:
        return None
    img=qrcode.make(f"rwgps://app/code/{code}")
    buf=io.BytesIO(); img.save(buf,format="PNG")
    return "data:image/png;base64,"+base64.b64encode(buf.getvalue()).decode()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--template",required=True); ap.add_argument("--notes",default="")
    ap.add_argument("--knp-json",default="")
    ap.add_argument("--blank",action="store_true")
    ap.add_argument("--out",default="brief.html"); ap.add_argument("--lang",default="nl")
    for f in ["day","title","sub","dist","ship","phone","pill-class","pill-text","qr-code","start","arrival"]:
        ap.add_argument("--"+f,default="")
    a=ap.parse_args()
    L=LABELS.get(a.lang,LABELS["nl"])
    hml=open(a.template,encoding="utf-8").read()
    if a.knp_json:
        import json
        kd=json.load(open(a.knp_json,encoding="utf-8"))
        knp=[{"kp":str(x["kp"]),"km":float(x["km"])} for x in kd.get("knooppunten",[])]; pois=[]; gaps=[]
    else:
        pois,knp,gaps=parse_notes(open(a.notes,encoding="utf-8").read())

    # header fields
    if a.day:   hml=re.sub(r'(<span class="day">)[^<]*(</span>)',lambda m:m.group(1)+esc(a.day)+m.group(2),hml,1)
    if a.pill_text: hml=re.sub(r'<span class="route-pill [^"]*">[^<]*</span>',
                    f'<span class="route-pill {esc(a.pill_class or "red")}">{esc(a.pill_text)}</span>',hml,1)
    if a.title: 
        sub=f' <span style="font-size:16px;color:#7a8394">{esc(a.sub)}</span>' if a.sub else ""
        hml=re.sub(r'<h1>.*?</h1>',f'<h1>{esc(a.title)}{sub}</h1>',hml,1,re.S)
    if a.dist:  hml=re.sub(r'(<div class="dist">)[^<]*(</div>)',lambda m:m.group(1)+esc(a.dist)+m.group(2),hml,1)
    if a.ship:  hml=re.sub(r'(<div class="callship">).*?(</div>)',
                    lambda m:m.group(1)+L["callship"].format(s=esc(a.ship),p=esc(a.phone))+m.group(2),hml,1,re.S)
    qr=qr_datauri(a.qr_code) if a.qr_code else None
    if qr: hml=re.sub(r'(<div class="qr"><img src=")[^"]*(")',lambda m:m.group(1)+qr+m.group(2),hml,1)

    # dynamic blocks
    hml=replace_block(hml,r'<div class="timeline">',timeline_html(knp,a.start.split(",")[0],a.arrival.split(",")[0]),r'<h2>')
    cards={"lunch":[],"high":[],"safe":[],"know":[]}
    if a.blank:
        route_html='<div class="route"><p style="color:#7a8394"><i>'+L["todo"]+'</i></p></div>'
    else:
        route_html=route_draft(knp,gaps,L)
    hml=replace_block(hml,r'<div class="route">',route_html,r'<h2>')
    if a.blank:
        # no POI info -> drop the empty Onderweg title + info section entirely
        hml=re.sub(r'<h2>[^<]*</h2>\s*<div class="info">.*?</div>\s*(?=<footer)','',hml,count=1,flags=re.S)
    else:
        cards=poi_cards(pois,L); order=["lunch","high","safe","know"]; idx=[0]
        def fill_ul(m):
            c=order[idx[0]] if idx[0]<len(order) else "know"; idx[0]+=1
            items="\n".join(cards[c]) or f'<li style="color:#7a8394"><i>{L["todo"]}</i></li>'
            return m.group(1)+"\n"+items+"\n"+m.group(2)
        hml=re.sub(r'(<ul>).*?(</ul>)',fill_ul,hml,count=4,flags=re.S)
    # footer + section labels
    if a.start or a.arrival:
        ts=[a.start,a.arrival]; fi=[0]
        def _ft(m):
            loc=ts[fi[0]] if fi[0]<len(ts) else ""; fi[0]+=1
            return f'{m.group(1)}{esc(loc)}{m.group(2)}' if loc else m.group(0)
        hml=re.sub(r'(<div class="t"><b>[^<]*</b>)[^<]*(</div>)',_ft,hml,count=2)
    open(a.out,"w",encoding="utf-8").write(hml)
    print(f"parsed: {len(knp)} knp nodes | {len(gaps)} gaps | {len(pois)} POIs")
    print(f"POI cards: "+", ".join(f'{k}={len(v)}' for k,v in cards.items()))
    print(f"QR: {'generated' if qr else 'skipped (no code or qrcode lib)'}")
    print(f"wrote {a.out}  (timeline+header auto; route prose & POI cards are DRAFTS to trim)")

if __name__=="__main__": main()
