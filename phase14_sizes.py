import os
files=['MANDI_46','MANDI_47','MANDI_53','MANDI_54','MANDI_55','MANDI_56']
root=r'H:\TESTING CONTIUES\FUNIVIA\NT219'
import laspy
tot=0; totpts=0
for f in files:
    p=os.path.join(root,f+'.laz'); L=os.path.getsize(p)
    with laspy.open(p) as z:
        hdr=z.header; c=hdr.point_count
    tot+=L; totpts+=c
    print(f'{f}.laz: bytes={L:>14,}  GiB={L/1073741824:.3f}  points={c:,}')
print(f'TOTAL: bytes={tot:,}  GiB={tot/1073741824:.3f}  points={totpts:,}')
