//+------------------------------------------------------------------+
//| ClaudebotSMC.mq5                                                  |
//| Same SMC rules as the Claudebot Python bot (signals/smc.py):      |
//|   bias   : last close through a confirmed swing on the trend TF   |
//|   entry  : sweep of a swing low/high -> close through the swing   |
//|            before it (CHoCH) -> first retest of the FVG left by   |
//|            that move                                              |
//|   stop   : beyond the sweep wick, target = RewardRisk x stop      |
//| Attach to an M1 chart. Draws arrows, the FVG box and SL/TP/entry. |
//| Visual + alerts only: it never places orders.                     |
//+------------------------------------------------------------------+
#property copyright   "Claudebot"
#property version     "1.00"
#property description "SMC scalp setups: trend-TF bias, sweep -> CHoCH -> FVG retest."
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   2
#property indicator_label1  "Buy setup"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrDodgerBlue
#property indicator_width1  2
#property indicator_label2  "Sell setup"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrOrangeRed
#property indicator_width2  2

input ENUM_TIMEFRAMES InpTrendTF     = PERIOD_M5; // Trend timeframe (bias)
input int             InpTrendBars   = 300;       // Trend bars used for bias
input int             InpEntryBars   = 150;       // Entry bars scanned per setup
input int             InpTrendSwing  = 3;         // Trend swing length (bars each side)
input int             InpEntrySwing  = 2;         // Entry swing length (bars each side)
input int             InpMaxSetupAge = 15;        // Max bars since the structure break
input double          InpSweepMaxAtr = 1.5;       // Max sweep depth (ATR)
input double          InpFvgMinAtr   = 0.05;      // Min FVG size (ATR)
input double          InpSlBufferAtr = 0.1;       // Stop buffer beyond the wick (ATR)
input double          InpMinSlAtr    = 0.5;       // Min stop distance (ATR)
input double          InpMaxSlAtr    = 4.0;       // Max stop distance (ATR)
input double          InpRewardRisk  = 1.8;       // Target in R
input int             InpAtrPeriod   = 14;        // ATR period
input int             InpHistoryBars = 2000;      // Bars of history to mark on load
input bool            InpDrawLevels  = true;      // Draw FVG box and SL/TP/entry lines
input int             InpLevelBars   = 20;        // Length of the SL/TP lines (bars)
input bool            InpAlert       = true;      // Pop-up alert on a new setup
input bool            InpPush        = false;     // Push to the MT5 mobile app

double   BuyBuf[], SellBuf[];
datetime g_lastAlert = 0;
datetime g_biasKey   = 0;
int      g_bias      = 0;
const string PFX = "CBSMC_";

int OnInit()
  {
   SetIndexBuffer(0, BuyBuf, INDICATOR_DATA);
   SetIndexBuffer(1, SellBuf, INDICATOR_DATA);
   PlotIndexSetInteger(0, PLOT_ARROW, 233);
   PlotIndexSetInteger(1, PLOT_ARROW, 234);
   PlotIndexSetInteger(0, PLOT_ARROW_SHIFT, 12);
   PlotIndexSetInteger(1, PLOT_ARROW_SHIFT, -12);
   PlotIndexSetDouble(0, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(1, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   IndicatorSetString(INDICATOR_SHORTNAME, "Claudebot SMC");
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0, PFX);
  }

//--- fractal swings; a swing at i is only known at i + L --------------
int FindSwings(const double &h[], const double &l[], const int n, const int L,
               int &idx[], double &px[], bool &isHigh[])
  {
   ArrayResize(idx, 0); ArrayResize(px, 0); ArrayResize(isHigh, 0);
   int k = 0;
   for(int i = L; i < n - L; i++)
     {
      bool sh = true, sl = true;
      for(int j = i - L; j < i; j++)
        {
         if(h[j] >= h[i]) sh = false;
         if(l[j] <= l[i]) sl = false;
        }
      for(int j = i + 1; j <= i + L; j++)
        {
         if(h[j] > h[i]) sh = false;
         if(l[j] < l[i]) sl = false;
        }
      if(sh)
        {
         ArrayResize(idx, k + 1); ArrayResize(px, k + 1); ArrayResize(isHigh, k + 1);
         idx[k] = i; px[k] = h[i]; isHigh[k] = true; k++;
        }
      if(sl)
        {
         ArrayResize(idx, k + 1); ArrayResize(px, k + 1); ArrayResize(isHigh, k + 1);
         idx[k] = i; px[k] = l[i]; isHigh[k] = false; k++;
        }
     }
   return(k);
  }

double Atr(const double &h[], const double &l[], const double &c[], const int n, const int period)
  {
   if(n < 2) return(0.0);
   int start = MathMax(1, n - period);
   double sum = 0.0; int m = 0;
   for(int i = start; i < n; i++)
     {
      sum += MathMax(h[i], c[i - 1]) - MathMin(l[i], c[i - 1]);
      m++;
     }
   return(sum / m);
  }

//--- +1 up, -1 down, 0 none ------------------------------------------
int StructureBias(const double &h[], const double &l[], const double &c[], const int n, const int L)
  {
   int idx[]; double px[]; bool hi[];
   int ns = FindSwings(h, l, n, L, idx, px, hi);
   int k = 0, bias = 0;
   bool haveH = false, haveL = false;
   double H = 0, Lo = 0;
   for(int i = 0; i < n; i++)
     {
      while(k < ns && idx[k] + L <= i)
        {
         if(hi[k]) { H = px[k]; haveH = true; }
         else      { Lo = px[k]; haveL = true; }
         k++;
        }
      if(haveH && c[i] > H)  { bias = 1;  haveH = false; }
      if(haveL && c[i] < Lo) { bias = -1; haveL = false; }
     }
   return(bias);
  }

//--- bias from trend-TF bars that had CLOSED by `cutoff` --------------
int BiasAt(const datetime cutoff)
  {
   int tf = PeriodSeconds(InpTrendTF);
   datetime lastOpen = cutoff - tf;
   long     secs = (long)lastOpen;
   datetime key  = (datetime)(secs - secs % tf);
   if(key == g_biasKey) return(g_bias);
   MqlRates r[];
   int got = CopyRates(_Symbol, InpTrendTF, lastOpen, InpTrendBars, r);
   if(got < 4 * InpTrendSwing + 5) return(0);  // history not loaded yet: retry later
   double h[], l[], c[];
   ArrayResize(h, got); ArrayResize(l, got); ArrayResize(c, got);
   for(int i = 0; i < got; i++) { h[i] = r[i].high; l[i] = r[i].low; c[i] = r[i].close; }
   g_bias = StructureBias(h, l, c, got, InpTrendSwing);
   g_biasKey = key;
   return(g_bias);
  }

//--- long setup on the last bar of the window (shorts: mirrored prices)
bool LongSetup(const double &h[], const double &l[], const double &c[], const int n,
               double &entry, double &sl, double &tp, double &fvgLo, double &fvgHi,
               double &swept, double &broken, int &brkOut)
  {
   int L = InpEntrySwing;
   if(n < MathMax(InpAtrPeriod + 1, 4 * L + 5)) return(false);
   double a = Atr(h, l, c, n, InpAtrPeriod);
   if(a <= 0) return(false);
   int idx[]; double px[]; bool hi[];
   int ns = FindSwings(h, l, n, L, idx, px, hi);
   int t = n - 1;
   int loBrk = MathMax(t - InpMaxSetupAge, L + 1);

   for(int brk = t - 1; brk >= loBrk; brk--)
     {
      int hk = -1;
      for(int k = 0; k < ns; k++)
         if(hi[k] && idx[k] + L <= brk) hk = k;
      if(hk < 0) continue;
      double Hp = px[hk]; int Hi = idx[hk];
      if(!(c[brk] > Hp && Hp >= c[brk - 1])) continue;
      bool earlier = false;
      for(int j = Hi + 1; j < brk; j++)
         if(c[j] > Hp) { earlier = true; break; }
      if(earlier) continue;

      //--- found the first break of this high: evaluate it, no fallback
      int lk = -1;
      for(int k = 0; k < ns; k++)
         if(!hi[k] && idx[k] < Hi) lk = k;
      if(lk < 0) return(false);
      double liq = px[lk];

      int s = Hi + 1;
      for(int i = Hi + 1; i <= brk; i++)
         if(l[i] < l[s]) s = i;
      double sweepLow = l[s];
      if(!(sweepLow < liq)) return(false);
      if(liq - sweepLow > InpSweepMaxAtr * a) return(false);

      bool found = false; double bottom = 0, top = 0;
      for(int i = brk; i >= s + 2; i--)
        {
         double gap = l[i] - h[i - 2];
         if(gap > 0 && gap >= InpFvgMinAtr * a) { bottom = h[i - 2]; top = l[i]; found = true; break; }
        }
      if(!found) return(false);

      for(int j = brk + 1; j < t; j++)
         if(l[j] <= top) return(false);          // already retested
      if(!(l[t] <= top && c[t] >= bottom && l[t] > sweepLow)) return(false);

      entry = c[t];
      sl = MathMin(sweepLow - InpSlBufferAtr * a, entry - InpMinSlAtr * a);
      double risk = entry - sl;
      if(risk <= 0 || risk > InpMaxSlAtr * a) return(false);
      tp = entry + InpRewardRisk * risk;
      fvgLo = bottom; fvgHi = top; swept = liq; broken = Hp; brkOut = brk;
      return(true);
     }
   return(false);
  }

//--- drawing ------------------------------------------------------------
void Line(const string name, const datetime t1, const datetime t2, const double p, const color clr,
          const ENUM_LINE_STYLE style)
  {
   ObjectDelete(0, name);
   ObjectCreate(0, name, OBJ_TREND, 0, t1, p, t2, p);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
  }

void DrawSetup(const bool buy, const datetime tBrk, const datetime tSig, const double fvgLo,
               const double fvgHi, const double entry, const double sl, const double tp)
  {
   string id = PFX + (buy ? "B_" : "S_") + IntegerToString((long)tSig);
   datetime tEnd = tSig + InpLevelBars * PeriodSeconds();
   string box = id + "_fvg";
   ObjectDelete(0, box);
   ObjectCreate(0, box, OBJ_RECTANGLE, 0, tBrk, fvgHi, tSig, fvgLo);
   ObjectSetInteger(0, box, OBJPROP_COLOR, buy ? clrLightSkyBlue : clrLightSalmon);
   ObjectSetInteger(0, box, OBJPROP_FILL, true);
   ObjectSetInteger(0, box, OBJPROP_BACK, true);
   ObjectSetInteger(0, box, OBJPROP_SELECTABLE, false);
   Line(id + "_en", tSig, tEnd, entry, clrSilver, STYLE_DOT);
   Line(id + "_sl", tSig, tEnd, sl, clrRed, STYLE_SOLID);
   Line(id + "_tp", tSig, tEnd, tp, clrLimeGreen, STYLE_SOLID);
  }

//--- main ---------------------------------------------------------------
int OnCalculate(const int rates_total, const int prev_calculated, const datetime &time[],
                const double &open[], const double &high[], const double &low[],
                const double &close[], const long &tick_volume[], const long &volume[],
                const int &spread[])
  {
   int n = InpEntryBars;
   if(rates_total < n + 2) return(0);
   int last = rates_total - 2;   // last CLOSED bar; rates_total-1 is still forming
   int first;
   if(prev_calculated == 0)
     {
      ArrayInitialize(BuyBuf, EMPTY_VALUE);
      ArrayInitialize(SellBuf, EMPTY_VALUE);
      ObjectsDeleteAll(0, PFX);
      first = MathMax(n - 1, last - InpHistoryBars);
     }
   else
      first = MathMax(n - 1, prev_calculated - 2);
   BuyBuf[rates_total - 1] = EMPTY_VALUE;
   SellBuf[rates_total - 1] = EMPTY_VALUE;

   double wh[], wl[], wc[];
   ArrayResize(wh, n); ArrayResize(wl, n); ArrayResize(wc, n);
   int barSec = PeriodSeconds();

   for(int t = first; t <= last; t++)
     {
      BuyBuf[t] = EMPTY_VALUE;
      SellBuf[t] = EMPTY_VALUE;
      int bias = BiasAt(time[t] + barSec);
      if(bias == 0) continue;
      int s = t - n + 1;
      for(int i = 0; i < n; i++)
        {
         if(bias > 0) { wh[i] = high[s + i]; wl[i] = low[s + i];  wc[i] = close[s + i]; }
         else         { wh[i] = -low[s + i]; wl[i] = -high[s + i]; wc[i] = -close[s + i]; }
        }
      double e, sl, tp, lo, hi, swept, broken; int brk;
      if(!LongSetup(wh, wl, wc, n, e, sl, tp, lo, hi, swept, broken, brk)) continue;
      bool buy = bias > 0;
      if(!buy)
        {
         e = -e; sl = -sl; tp = -tp; swept = -swept; broken = -broken;
         double b = -hi; hi = -lo; lo = b;
        }
      if(buy) BuyBuf[t] = low[t]; else SellBuf[t] = high[t];
      if(InpDrawLevels) DrawSetup(buy, time[s + brk], time[t], lo, hi, e, sl, tp);

      if(t == last && prev_calculated > 0 && time[t] != g_lastAlert)
        {
         g_lastAlert = time[t];
         string msg = StringFormat("Claudebot SMC %s %s @ %s  SL %s  TP %s", _Symbol,
                                   buy ? "BUY" : "SELL", DoubleToString(e, _Digits),
                                   DoubleToString(sl, _Digits), DoubleToString(tp, _Digits));
         if(InpAlert) Alert(msg);
         if(InpPush)  SendNotification(msg);
        }
     }
   return(rates_total);
  }
//+------------------------------------------------------------------+
