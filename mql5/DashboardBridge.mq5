//+------------------------------------------------------------------+
//| DashboardBridge.mq5 - read-only bridge for Dashboardv1           |
//| Never sends, modifies or closes trading orders.                   |
//+------------------------------------------------------------------+
#property copyright "Dashboardv1"
#property version   "1.00"
#property service

input int PollIntervalMilliseconds = 1000;

string ROOT      = "Dashboardv1\\";
string REQUESTS  = "Dashboardv1\\Requests\\";
string RESPONSES = "Dashboardv1\\Responses\\";

string JsonEscape(const string value)
{
   string result=value;
   StringReplace(result,"\\","\\\\");
   StringReplace(result,"\"","\\\"");
   StringReplace(result,"\r","\\r");
   StringReplace(result,"\n","\\n");
   StringReplace(result,"\t","\\t");
   return result;
}

string IsoUtc()
{
   MqlDateTime parts={};
   TimeToStruct(TimeGMT(),parts);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",parts.year,parts.mon,parts.day,parts.hour,parts.min,parts.sec);
}

bool WriteAtomic(const string target,const string content)
{
   string temp=target+".tmp";
   int handle=FileOpen(temp,FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(handle==INVALID_HANDLE)
   {
      Print("DashboardBridge: FileOpen failed ",temp," error=",GetLastError());
      return false;
   }
   FileWriteString(handle,content);
   FileFlush(handle);
   FileClose(handle);
   if(FileIsExist(target))
      FileDelete(target);
   if(!FileMove(temp,0,target,FILE_REWRITE))
   {
      Print("DashboardBridge: FileMove failed ",target," error=",GetLastError());
      return false;
   }
   return true;
}

string ReadText(const string path)
{
   int handle=FileOpen(path,FILE_READ|FILE_TXT|FILE_ANSI);
   if(handle==INVALID_HANDLE)
      return "";
   string value="";
   while(!FileIsEnding(handle))
      value+=FileReadString(handle);
   FileClose(handle);
   return value;
}

string JsonString(const string source,const string key,const string fallback="")
{
   string marker="\""+key+"\"";
   int pos=StringFind(source,marker);
   if(pos<0) return fallback;
   pos=StringFind(source,":",pos+StringLen(marker));
   if(pos<0) return fallback;
   int start=StringFind(source,"\"",pos+1);
   if(start<0) return fallback;
   int finish=StringFind(source,"\"",start+1);
   if(finish<0) return fallback;
   return StringSubstr(source,start+1,finish-start-1);
}

long JsonLong(const string source,const string key,const long fallback=0)
{
   string marker="\""+key+"\"";
   int pos=StringFind(source,marker);
   if(pos<0) return fallback;
   pos=StringFind(source,":",pos+StringLen(marker));
   if(pos<0) return fallback;
   pos++;
   while(pos<StringLen(source) && StringGetCharacter(source,pos)==' ') pos++;
   int finish=pos;
   while(finish<StringLen(source))
   {
      ushort ch=StringGetCharacter(source,finish);
      if((ch<'0' || ch>'9') && ch!='-') break;
      finish++;
   }
   if(finish<=pos) return fallback;
   return StringToInteger(StringSubstr(source,pos,finish-pos));
}

string DealTypeName(const ENUM_DEAL_TYPE type)
{
   if(type==DEAL_TYPE_BUY) return "BUY";
   if(type==DEAL_TYPE_SELL) return "SELL";
   return "OTHER";
}

string EntryTypeName(const ENUM_DEAL_ENTRY entry)
{
   if(entry==DEAL_ENTRY_IN) return "IN";
   if(entry==DEAL_ENTRY_OUT) return "OUT";
   if(entry==DEAL_ENTRY_INOUT) return "INOUT";
   if(entry==DEAL_ENTRY_OUT_BY) return "OUT_BY";
   return "UNKNOWN";
}

string DealJson(const ulong ticket)
{
   long position_id=HistoryDealGetInteger(ticket,DEAL_POSITION_ID);
   long time_msc=HistoryDealGetInteger(ticket,DEAL_TIME_MSC);
   long magic=HistoryDealGetInteger(ticket,DEAL_MAGIC);
   ENUM_DEAL_TYPE deal_type=(ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket,DEAL_TYPE);
   ENUM_DEAL_ENTRY entry_type=(ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket,DEAL_ENTRY);
   return StringFormat(
      "{\"ticket\":%I64u,\"position_id\":%I64d,\"time_msc\":%I64d,\"symbol\":\"%s\",\"deal_type\":\"%s\",\"entry_type\":\"%s\",\"volume\":%.8f,\"price\":%.10f,\"profit\":%.8f,\"commission\":%.8f,\"swap\":%.8f,\"magic\":%I64d,\"comment\":\"%s\"}",
      ticket,position_id,time_msc,JsonEscape(HistoryDealGetString(ticket,DEAL_SYMBOL)),DealTypeName(deal_type),EntryTypeName(entry_type),
      HistoryDealGetDouble(ticket,DEAL_VOLUME),HistoryDealGetDouble(ticket,DEAL_PRICE),HistoryDealGetDouble(ticket,DEAL_PROFIT),
      HistoryDealGetDouble(ticket,DEAL_COMMISSION),HistoryDealGetDouble(ticket,DEAL_SWAP),magic,JsonEscape(HistoryDealGetString(ticket,DEAL_COMMENT))
   );
}

string PositionJson(const ulong ticket)
{
   if(!PositionSelectByTicket(ticket)) return "";
   ENUM_POSITION_TYPE type=(ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
   return StringFormat(
      "{\"ticket\":%I64u,\"position_id\":%I64u,\"symbol\":\"%s\",\"direction\":\"%s\",\"time_msc\":%I64d,\"volume\":%.8f,\"open_price\":%.10f,\"current_price\":%.10f,\"profit\":%.8f,\"swap\":%.8f,\"magic\":%I64d,\"comment\":\"%s\"}",
      ticket,ticket,JsonEscape(PositionGetString(POSITION_SYMBOL)),type==POSITION_TYPE_BUY?"Long":"Short",PositionGetInteger(POSITION_TIME_MSC),
      PositionGetDouble(POSITION_VOLUME),PositionGetDouble(POSITION_PRICE_OPEN),PositionGetDouble(POSITION_PRICE_CURRENT),
      PositionGetDouble(POSITION_PROFIT),PositionGetDouble(POSITION_SWAP),PositionGetInteger(POSITION_MAGIC),JsonEscape(PositionGetString(POSITION_COMMENT))
   );
}

string OrderTypeName(const ENUM_ORDER_TYPE type)
{
   if(type==ORDER_TYPE_BUY_LIMIT) return "BUY_LIMIT";
   if(type==ORDER_TYPE_SELL_LIMIT) return "SELL_LIMIT";
   if(type==ORDER_TYPE_BUY_STOP) return "BUY_STOP";
   if(type==ORDER_TYPE_SELL_STOP) return "SELL_STOP";
   if(type==ORDER_TYPE_BUY_STOP_LIMIT) return "BUY_STOP_LIMIT";
   if(type==ORDER_TYPE_SELL_STOP_LIMIT) return "SELL_STOP_LIMIT";
   return EnumToString(type);
}

string OrderJson(const ulong ticket)
{
   if(!OrderSelect(ticket)) return "";
   ENUM_ORDER_TYPE type=(ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
   return StringFormat(
      "{\"ticket\":%I64u,\"symbol\":\"%s\",\"order_type\":\"%s\",\"time_msc\":%I64d,\"volume\":%.8f,\"price\":%.10f,\"magic\":%I64d,\"comment\":\"%s\"}",
      ticket,JsonEscape(OrderGetString(ORDER_SYMBOL)),OrderTypeName(type),OrderGetInteger(ORDER_TIME_SETUP_MSC),
      OrderGetDouble(ORDER_VOLUME_CURRENT),OrderGetDouble(ORDER_PRICE_OPEN),OrderGetInteger(ORDER_MAGIC),
      JsonEscape(OrderGetString(ORDER_COMMENT))
   );
}

void ProcessSync(const string request)
{
   bool terminal_connected=(bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   long since_msc=JsonLong(request,"since_msc",0);
   datetime from=(datetime)(since_msc/1000);
   if(from<=0) from=(datetime)0;
   datetime to=TimeCurrent();
   bool selected=HistorySelect(from,to);
   string deals="[";
   bool first=true;
   if(selected)
   {
      int total=HistoryDealsTotal();
      for(int i=0;i<total;i++)
      {
         ulong ticket=HistoryDealGetTicket(i);
         if(ticket==0) continue;
         long deal_msc=HistoryDealGetInteger(ticket,DEAL_TIME_MSC);
         ENUM_DEAL_TYPE type=(ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket,DEAL_TYPE);
         if(deal_msc<=since_msc || (type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL)) continue;
         if(!first) deals+=",";
         deals+=DealJson(ticket);
         first=false;
      }
   }
   deals+="]";

   string positions="[";
   first=true;
   int positions_total=PositionsTotal();
   for(int i=0;i<positions_total;i++)
   {
      ulong ticket=PositionGetTicket(i);
      string row=PositionJson(ticket);
      if(row=="") continue;
      if(!first) positions+=",";
      positions+=row;
      first=false;
   }
   positions+="]";

   string orders="[";
   first=true;
   int orders_total=OrdersTotal();
   for(int i=0;i<orders_total;i++)
   {
      ulong ticket=OrderGetTicket(i);
      string row=OrderJson(ticket);
      if(row=="") continue;
      if(!first) orders+=",";
      orders+=row;
      first=false;
   }
   orders+="]";

   string response=StringFormat(
      "{\"schema_version\":2,\"status\":\"ok\",\"terminal_connected\":%s,\"generated_at\":\"%s\",\"account_login\":\"%I64d\",\"server\":\"%s\",\"account\":{\"balance\":%.8f,\"equity\":%.8f,\"margin\":%.8f,\"free_margin\":%.8f},\"deals\":%s,\"positions\":%s,\"orders\":%s}",
      terminal_connected ? "true" : "false",IsoUtc(),AccountInfoInteger(ACCOUNT_LOGIN),JsonEscape(AccountInfoString(ACCOUNT_SERVER)),AccountInfoDouble(ACCOUNT_BALANCE),
      AccountInfoDouble(ACCOUNT_EQUITY),AccountInfoDouble(ACCOUNT_MARGIN),AccountInfoDouble(ACCOUNT_MARGIN_FREE),deals,positions,orders
   );
   WriteAtomic(RESPONSES+"sync.response.json",response);
}

ENUM_TIMEFRAMES ParseTimeframe(const string value)
{
   if(value=="M1") return PERIOD_M1;
   if(value=="M5") return PERIOD_M5;
   if(value=="M15") return PERIOD_M15;
   if(value=="M30") return PERIOD_M30;
   if(value=="H4") return PERIOD_H4;
   if(value=="D1") return PERIOD_D1;
   return PERIOD_H1;
}

void ProcessChart(const string request)
{
   string symbol=JsonString(request,"symbol",_Symbol);
   string timeframe_name=JsonString(request,"timeframe","H1");
   datetime from=(datetime)JsonLong(request,"from",0);
   datetime to=(datetime)JsonLong(request,"to",(long)TimeCurrent());
   ENUM_TIMEFRAMES timeframe=ParseTimeframe(timeframe_name);
   MqlRates rates[];
   ArraySetAsSeries(rates,false);
   int copied=CopyRates(symbol,timeframe,from,to,rates);
   string candles="[";
   for(int i=0;i<copied;i++)
   {
      if(i>0) candles+=",";
      candles+=StringFormat("{\"time\":%I64d,\"open\":%.10f,\"high\":%.10f,\"low\":%.10f,\"close\":%.10f,\"tick_volume\":%I64d}",(long)rates[i].time,rates[i].open,rates[i].high,rates[i].low,rates[i].close,rates[i].tick_volume);
   }
   candles+="]";
   string response=StringFormat("{\"schema_version\":1,\"status\":\"ok\",\"generated_at\":\"%s\",\"symbol\":\"%s\",\"timeframe\":\"%s\",\"candles\":%s}",IsoUtc(),JsonEscape(symbol),timeframe_name,candles);
   WriteAtomic(RESPONSES+"chart.response.json",response);
}

void EnsureFolders()
{
   FolderCreate("Dashboardv1");
   FolderCreate("Dashboardv1\\Requests");
   FolderCreate("Dashboardv1\\Responses");
   FolderCreate("Dashboardv1\\Responses\\Archive");
}

void OnStart()
{
   EnsureFolders();
   Print("DashboardBridge: read-only service started for account ",AccountInfoInteger(ACCOUNT_LOGIN));
   while(!IsStopped())
   {
      string sync_path=REQUESTS+"sync.request.json";
      if(FileIsExist(sync_path))
      {
         string request=ReadText(sync_path);
         if(request!="") ProcessSync(request);
         FileDelete(sync_path);
      }
      string chart_path=REQUESTS+"chart.request.json";
      if(FileIsExist(chart_path))
      {
         string request=ReadText(chart_path);
         if(request!="") ProcessChart(request);
         FileDelete(chart_path);
      }
      Sleep(MathMax(250,PollIntervalMilliseconds));
   }
   Print("DashboardBridge: service stopped");
}
