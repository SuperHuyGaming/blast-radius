import json,sys
data,ans,out=sys.argv[1:4]
T={t['number']:round(t['seconds']*1000) for t in json.load(open(data+'/tests.json'))['tests']}
A=json.load(open(ans))['commits']
def cost(ws):
    ws=[w for w in ws if w]
    return 0 if not ws else max(sum(T[x] for x in w) for w in ws)+2000*len(ws)
def lpt(f):
    best=0 if not f else 1e18
    s=sorted((T[x] for x in f),reverse=True)
    for k in range(1,9):
        L=[0]*k
        for x in s: L[L.index(min(L))]+=x
        if f: best=min(best,max(L)+2000*k)
    return best
tot=0
for line in open(out):
    o=json.loads(line); c=o['commit']; ws=o['workers']
    sel={x for w in ws for x in w}; f=set(A[c]['failing'])
    miss=bool(f-sel); my=cost(ws); orc=lpt(f)
    if my-orc>1 or miss: print(c,A[c]['kind'],'mine %.2f oracle %.2f'%(my/1000,orc/1000),'MISS '+str(sorted(f-sel)[:6]) if miss else '')
    tot+=my-orc
print('excess over oracle %.2f'%(tot/1000))
