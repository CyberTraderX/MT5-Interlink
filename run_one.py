import sys,os
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import headless_pool as hp
asset,cfg=sys.argv[1],sys.argv[2]
P={'NDX':{'EMA_Period':20,'ATR_Multiplier':2.5,'TP_ATR_Mult':4.0,'RiskPercent':1.22,'MagicNumber':900049,'_sym':'NDX100'},
   'JPY':{'EMA_Period':20,'ATR_Multiplier':2.5,'TP_ATR_Mult':4.0,'RiskPercent':0.5,'MagicNumber':900048,'_sym':'USDJPY'}}
base=dict(P[asset]); sym=base.pop('_sym')
if cfg=='stop': base['InpTightStopR']=0.5
elif cfg=='volstop': base['InpUseVolFilter']=True; base['InpTightStopR']=0.5
r=hp.run_task({'expert':'KBTC_ult.ex5','symbol':sym,'period':'H1','frm':'2020.01.01','to':'2026.06.01','model':'0','deposit':'200000','leverage':'100','inputs':base},700)
print('%-4s %-8s net=$%-9s ret=%3s%% PF=%-5s eqDD=%-6s MAR=%-6s tr=%-4s ok=%s'%(asset,cfg,format(r.get('net_profit',0) or 0,',.0f'),round(r.get('return_pct') or 0),r.get('profit_factor'),r.get('equity_dd_rel_pct'),r.get('mar'),r.get('trades'),r.get('ok')))
