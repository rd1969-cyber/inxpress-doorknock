from PIL import Image, ImageDraw, ImageFont
import os

FR = r"C:\Users\rober\inxpress-doorknock\_frames"
NAVY=(26,58,92); TEAL=(46,139,106); WHITE=(255,255,255)

def font(sz, bold=True):
    for p in [r"C:\Windows\Fonts\segoeui"+("b" if bold else "")+".ttf",
              r"C:\Windows\Fonts\arial"+("bd" if bold else "")+".ttf"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, sz)
    return ImageFont.load_default()

F_CAP=font(27, True); F_SUB=font(20, False); F_BIG=font(46, True); F_MED=font(26, False)

def wrap(draw, text, fnt, maxw):
    words=text.split(); lines=[]; cur=""
    for w in words:
        t=(cur+" "+w).strip()
        if draw.textlength(t, font=fnt)<=maxw: cur=t
        else: lines.append(cur); cur=w
    if cur: lines.append(cur)
    return lines

W=820; CAP=104  # frame width, caption bar height
FH=1180         # frame height from chrome window

def captioned(name, text):
    frame=Image.open(os.path.join(FR,name+".png")).convert("RGB")
    frame=frame.resize((W,FH))
    canvas=Image.new("RGB",(W,FH+CAP),NAVY)
    canvas.paste(frame,(0,0))
    d=ImageDraw.Draw(canvas)
    lines=wrap(d,text,F_CAP,W-60)
    total=sum(F_CAP.size+6 for _ in lines)-6
    y=FH+(CAP-total)//2
    for ln in lines:
        w=d.textlength(ln,font=F_CAP)
        d.text(((W-w)//2,y),ln,font=F_CAP,fill=WHITE); y+=F_CAP.size+6
    return canvas

def textcard(title, sub):
    canvas=Image.new("RGB",(W,FH+CAP),NAVY)
    d=ImageDraw.Draw(canvas)
    # teal accent bar
    d.rectangle([0,(FH+CAP)//2-120,W,(FH+CAP)//2-116],fill=TEAL)
    tl=wrap(d,title,F_BIG,W-100); y=(FH+CAP)//2-90
    for ln in tl:
        w=d.textlength(ln,font=F_BIG); d.text(((W-w)//2,y),ln,font=F_BIG,fill=WHITE); y+=F_BIG.size+8
    y+=20
    for ln in wrap(d,sub,F_MED,W-140):
        w=d.textlength(ln,font=F_MED); d.text(((W-w)//2,y),ln,font=F_MED,fill=(200,215,230)); y+=F_MED.size+8
    return canvas

frames=[
    (textcard("InXpress Sales Tools","A 60-second look — everything wired live to HubSpot"), 2600),
    (captioned("home","Three tools, one place. Reps log in once per device."), 3200),
    (captioned("knock","Door Knock (phone): log a walk-in in seconds. Chips light up as they qualify, one tap sets the disposition."), 4200),
    (captioned("qualify","Qualification Sheet (laptop): search a company, it pulls straight from HubSpot and pre-fills everything."), 4200),
    (captioned("customer","Customer Update (laptop): fix a customer's info fast — their status is never touched."), 4200),
    (textcard("That's it","Writes back to HubSpot - no duplicate deals - statuses protected.  Feedback welcome!"), 3400),
]

# downscale for a shareable file size
SCALE=0.62
imgs=[]; durs=[]
for im,ms in frames:
    im=im.resize((int(W*SCALE),int((FH+CAP)*SCALE)))
    imgs.append(im.convert("P",palette=Image.ADAPTIVE,colors=128)); durs.append(ms)

out_project=r"C:\Users\rober\inxpress-doorknock\InXpress-walkthrough.gif"
imgs[0].save(out_project, save_all=True, append_images=imgs[1:], duration=durs, loop=0, optimize=True, disposal=2)
sz=os.path.getsize(out_project)//1024
print("saved:", out_project, "(", sz, "KB )")

# also drop a copy on the Desktop if it exists
desk=r"C:\Users\rober\Desktop"
if os.path.isdir(desk):
    import shutil; shutil.copy(out_project, os.path.join(desk,"InXpress-walkthrough.gif")); print("copied to Desktop")
else:
    print("no Desktop folder found")
