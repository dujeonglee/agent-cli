#include <SDL2/SDL.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#define SW 960, SH 600, MS 24, TS 64, NSPR 6
#define FOV (M_PI/3), HFOV (FOV/2), SPD 0.035, RSPD 0.025, MDEPTH 30.0
static int mp[MS][MS]={
{1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1},
{1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1},
{1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1},
{1,0,0,2,2,2,2,0,0,0,0,0,0,0,0,3,3,3,3,0,0,0,0,1},
{1,0,0,2,0,0,0,0,0,0,0,0,0,0,0,0,0,0,3,0,0,0,0,1},
{1,0,0,2,0,0,0,0,0,0,0,0,0,0,0,0,0,0,3,0,0,0,0,1},
{1,0,0,2,2,0,0,0,0,0,0,0,0,0,0,0,0,3,3,0,0,0,0,1},
{1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1},
{1,0,0,0,0,0,0,0,4,4,0,0,0,0,4,4,0,0,0,0,0,0,0,1},
{1,0,0,0,0,0,0,0,4,0,0,0,0,0,0,4,0,0,0,0,0,0,0,1},
{1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1},
{1,0,0,0,0,0,0,0,0,0,0,5,5,0,0,0,0,0,0,0,0,0,0,1},
{1,0,0,0,0,0,0,0,0,0,0,5,5,0,0,0,0,0,0,0,0,0,0,1},
{1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1},
{1,0,0,0,0,0,0,0,4,0,0,0,0,0,0,4,0,0,0,0,0,0,0,1},
{1,0,0,0,0,0,0,0,4,4,0,0,0,0,4,4,0,0,0,0,0,0,0,1},
{1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1},
{1,0,0,3,3,0,0,0,0,0,0,0,0,0,0,0,0,2,2,0,0,0,0,1},
{1,0,0,0,0,0,3,0,0,0,0,0,0,0,0,0,0,0,2,0,0,0,0,1},
{1,0,0,0,0,0,3,0,0,0,0,0,0,0,0,0,0,0,2,0,0,0,0,1},
{1,0,0,3,3,3,3,0,0,0,0,0,0,0,0,0,2,2,2,2,0,0,0,1},
{1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1},
{1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1},
{1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1}};
static Uint32 cc(int v){return v<0?0:v>255?255:(Uint32)v;}
typedef struct{Uint32 p[TS][TS];}Tex;Tex tex[6];
void gen_brick(int t,Uint32 r,Uint32 g,Uint32 b){for(int y=0;y<TS;y++)for(int x=0;x<TS;x++){int bh=16,bw=32,row=y/bh,off=(row%2)*(bw/2),bx=(x+off)%bw,by=y%bh,m=(bx==0)||(by==0);Uint32 cr=m?60:cc(r+((x*7+y*13+t*31)%30-15)),cg=m?60:cc(g+((x*7+y*13+t*31)%30-15)),cb=m?60:cc(b+((x*7+y*13+t*31)%30-15));tex[t].p[y][x]=(0xFF<<24)|(cr<<16)|(cg<<8)|cb;}}
void gen_stone(int t,Uint32 r,Uint32 g,Uint32 b){for(int y=0;y<TS;y++)for(int x=0;x<TS;x++){int n=(x*11+y*17+t*41)%50-25,n2=(x*3+y*7+t*19)%30-15;tex[t].p[y][x]=(0xFF<<24)|(cc(r+n)<<16)|(cc(g+n)<<8)|cc(b+n2);}}
void gen_metal(int t,Uint32 r,Uint32 g,Uint32 b){for(int y=0;y<TS;y++)for(int x=0;x<TS;x++){int ph=32,pw=32,px2=x%pw,py2=y%ph,bd=(px2==0||px2==pw-1||py2==0||py2==ph-1);Uint32 cr,cg,cb;if(bd){cr=100;cg=100;cb=110;}else{int n=(x*5+y*11+t*23)%20-10;cr=cc(r+n);cg=cc(g+n);cb=cc(b+n);if((px2==4||px2==pw-5)&&(py2==4||py2==ph-5)){cr=180;cg=180;cb=190;}}tex[t].p[y][x]=(0xFF<<24)|(cr<<16)|(cg<<8)|cb;}}
void gen_floor(Uint32 r,Uint32 g,Uint32 b){for(int y=0;y<TS;y++)for(int x=0;x<TS;x++){int t=16,tx=x%t,ty=y%t,gr=(tx==0||ty==0);Uint32 cr=gr?40:cc(r+((x*9+y*13)%20-10)),cg=gr?40:cc(g+((x*9+y*13)%20-10)),cb=gr?45:cc(b+((x*9+y*13)%20-10));tex[0].p[y][x]=(0xFF<<24)|(cr<<16)|(cg<<8)|cb;}}
typedef struct{Uint32 p[TS][TS];int a;}STex;STex stex[3];
void gen_enemy(void){memset(stex[0].p,0,sizeof(stex[0].p));stex[0].a=1;int cx=TS/2,cy=TS/2+4;for(int y=0;y<TS;y++)for(int x=0;x<TS;x++){int dx=x-cx,dy=y-cy,d2=dx*dx+dy*dy;if(d2<100&&y>8&&y<TS-4)stex[0].p[y][x]=(0xFF<<24)|(180<<16)|(40<<8)|40;if(d2<64&&y>4&&y<20)stex[0].p[y][x]=(0xFF<<24)|(200<<16)|(60<<8)|30;if((x==cx-4&&y==cy-6)||(x==cx+4&&y==cy-6))stex[0].p[y][x]=(0xFF<<24)|(255<<16)|0;if((x==cx-6&&y<10&&y>2)||(x==cx+6&&y<10&&y>2))stex[0].p[y][x]=(0xFF<<24)|(160<<16)|(30<<8)|20;}}
void gen_pickup(int t,Uint32 r,Uint32 g,Uint32 b){memset(stex[t].p,0,sizeof(stex[t].p));stex[t].a=1;int cx=TS/2,cy=TS/2;for(int y=0;y<TS;y++)for(int x=0;x<TS;x++){int dx=x-cx,dy=y-cy;if(dx*dx+dy*dy<144){int n=(x*7+y*11)%30-15;stex[t].p[y][x]=(0xFF<<24)|(cc(r+n)<<16)|(cc(g+n)<<8)|cc(b+n);}}}
double plx=3.0,ply=3.0,pla=M_PI/4;int hp=100,am=50,sc=0,shoot=0,stm=0;
typedef struct{double x,y;int type,alive,hp;}Sprite;Sprite spr[NSPR]={{8.5,8.5,0,1,3},{15.5,5.5,0,1,3},{18.5,15.5,0,1,3},{10.5,18.5,0,1,3},{5.5,12.5,1,1,0},{14.5,12.5,2,1,0}};
double zbuf[SW];Uint32*sbuf;SDL_Renderer*rend;SDL_Texture*stex_s;SDL_KeyState*keys;int run=1;
void init_tex(void){gen_brick(1,160,60,50);gen_stone(2,120,120,110);gen_metal(3,80,90,100);gen_brick(4,100,130,60);gen_stone(5,140,100,60);gen_floor(80,75,70);gen_enemy();gen_pickup(1,50,200,50);gen_pickup(2,200,180,50);}
void draw_weapon(void){int wx=SW/2-40,wy=SH-160,bob=(int)(sin(SDL_GetTicks()/150.0)*5);if(shoot){for(int dy=-20;dy<20;dy++)for(int dx=-20;dx<20;dx++){int d2=dx*dx+dy*dy;if(d2<400){int sx=wx+80+dx,sy=wy-30+dy;if(sx>=0&&sx<SW&&sy>=0&&sy<SH){int br=(400-d2)/2;sbuf[sy*SW+sx]=(0xFF<<24)|(255<<16)|(cc(br)<<8)|0;}}}}for(int dy=0;dy<120;dy++)for(int dx=60;dx<100;dx++){int sx=wx+dx+bob,sy=wy+dy;if(sx>=0&&sx<SW&&sy>=0&&sy<SH){int sh=(dx>70&&dx<90)?60:40;sbuf[sy*SW+sx]=(0xFF<<24)|(sh<<16)|(sh<<8)|(sh+10);}}for(int dy=0;dy<60;dy++)for(int dx=70;dx<90;dx++){int sx=wx+dx+bob,sy=wy-40+dy;if(sx>=0&&sx<SW&&sy>=0&&sy<SH)sbuf[sy*SW+sx]=(0xFF<<24)|(30<<16)|(30<<8)|35;}}
void draw_hud(void){for(int y=SH-50;y<SH;y++)for(int x=0;x<SW;x++)sbuf[y*SW+x]=(0xFF<<24)|(50<<16)|(50<<8)|55;char hud[64];snprintf(hud,64,"HP:%d AMMO:%d SCORE:%d",hp,am,sc);int hx=20,hy=SH-35;for(int i=0;hud[i];i++){int ch=hud[i]-32;if(ch>=0&&ch<95){for(int dy=0;dy<12;dy++)for(int dx=0;dx<8;dx++){int sx=hx+i*10+dx,sy=hy+dy;if(sx>=0&&sx<SW&&sy>=0&&sy<SH){sbuf[sy*SW+sx]=(0xFF<<24)|(200<<16)|(200<<8)|200;}}}}}
void render(void){memset(zbuf,0,sizeof(zbuf));for(int x=0;x<SW;x++){double ra=pla-HFOV+(double)x/SW*FOV;double rdx=cos(ra),rdy=sin(ra);int mx=(int)px,my=(int)py;double ddx=(rdx==0)?1e30:fabs(1.0/rdx),ddy=(rdy==0)?1e30:fabs(1.0/rdy);double sdx,sdy;int sx,sy,side=0;if(rdx<0){sx=-1;sdx=(plx-mx)*ddx;}else{sx=1;sdx=(mx+1.0-plx)*ddx;}if(rdy<0){sy=-1;sdy=(ply-my)*ddy;}else{sy=1;sdy=(my+1.0-ply)*ddy;}int hit=0,wt=1;while(!hit){if(sdx<sdy){sdx+=ddx;mx+=sx;side=0;}else{sdy+=ddy;my+=sy;side=1;}if(mx<0||mx>=MS||my<0||my>=MS){hit=1;wt=1;}else if(mp[my][mx]>0){hit=1;wt=mp[my][mx];}}double pd=side==0?(sdx-ddx):(sdy-ddy);if(pd<0.01)pd=0.01;zbuf[x]=pd;int lh=(int)(SH/pd);int ds=SH/2-lh/2,de=SH/2+lh/2;if(ds<0)ds=0;if(de>=SH)de=SH-1;double wallX;if(side==0)wallX=ply+sdy-ddy;else wallX=plx+sdx-ddx;wallX-=floor(wallX);int tx=(int)(wallX*TS);if(tx<0)tx=0;if(tx>=TS)tx=TS-1;for(int y=ds;y<=de;y++){double t=(double)(y-ds)/(de-ds);int ty=(int)(t*TS);if(ty<0)ty=0;if(ty>=TS)ty=TS-