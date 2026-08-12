//+------------------------------------------------------------------+
//| BotInventoryReport.mq5                                           |
//| Read-only inventory of EAs attached to charts in the active      |
//| MetaTrader 5 profile. It never sends or modifies trading orders. |
//+------------------------------------------------------------------+
#property copyright "Dashboardv1"
#property version   "3.00"
#property script_show_inputs

input string CatalogFile = "BotInventoryReport\\BotInventoryCatalog.csv";
input string OutputFolder = "BotInventoryReport";
input bool   TrySharedCatalog = true;
input bool   AutoSeedCatalogFromHistory = true;

struct CatalogRow
{
   string ex5_name;
   string normalized_name;
   long   magic;
   string expected_comment;
   bool   duplicate;
   bool   shared_magic;
};

struct MagicStats
{
   string   symbol;
   long     magic;
   int      entries;
   datetime first_trade;
   datetime last_trade;
   string   deal_comments;
   int      orders;
   datetime first_order;
   datetime last_order;
   string   order_comments;
   string   order_states;
};

CatalogRow catalog[];
MagicStats history_stats[];
string warnings[];
string catalog_location="NO_ENCONTRADO";
int catalog_lines=0;
int catalog_valid_rows=0;
int catalog_exact_matches=0;
int catalog_auto_added=0;
ulong observed_order_tickets[];
int historical_orders_loaded=0;
int current_orders_loaded=0;

string Trim(const string value)
{
   string result=value;
   StringTrimLeft(result);
   StringTrimRight(result);
   return result;
}

string NormalizeEx5Name(const string value)
{
   string result=Trim(value);
   StringReplace(result,"/","\\");
   int last=-1;
   int pos=StringFind(result,"\\");
   while(pos>=0)
   {
      last=pos;
      pos=StringFind(result,"\\",pos+1);
   }
   if(last>=0)
      result=StringSubstr(result,last+1);
   StringToLower(result);
   int length=StringLen(result);
   if(length>4 && StringSubstr(result,length-4)==".ex5")
      result=StringSubstr(result,0,length-4);
   return Trim(result);
}

string DisplayEx5Name(const string value)
{
   string result=Trim(value);
   StringReplace(result,"/","\\");
   int last=-1;
   int pos=StringFind(result,"\\");
   while(pos>=0)
   {
      last=pos;
      pos=StringFind(result,"\\",pos+1);
   }
   if(last>=0)
      result=StringSubstr(result,last+1);
   int length=StringLen(result);
   if(result!="" && (length<4 || StringSubstr(StringToLowerCopy(result),length-4)!=".ex5"))
      result+=".ex5";
   return result;
}

string StringToLowerCopy(const string value)
{
   string result=value;
   StringToLower(result);
   return result;
}

void AddWarning(const string value)
{
   for(int i=0;i<ArraySize(warnings);i++)
      if(warnings[i]==value)
         return;
   int size=ArraySize(warnings);
   ArrayResize(warnings,size+1);
   warnings[size]=value;
}

string CsvEscape(const string value)
{
   string result=value;
   StringReplace(result,"\"","\"\"");
   return "\""+result+"\"";
}

string HtmlEscape(const string value)
{
   string result=value;
   StringReplace(result,"&","&amp;");
   StringReplace(result,"<","&lt;");
   StringReplace(result,">","&gt;");
   StringReplace(result,"\"","&quot;");
   StringReplace(result,"'","&#39;");
   return result;
}

string BoolText(const bool value) { return value ? "SI" : "NO"; }

string TimeText(const datetime value)
{
   if(value<=0) return "";
   return TimeToString(value,TIME_DATE|TIME_SECONDS);
}

string IsoUtc()
{
   MqlDateTime p={};
   TimeToStruct(TimeGMT(),p);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",p.year,p.mon,p.day,p.hour,p.min,p.sec);
}

bool ParseLongStrict(const string value,long &number)
{
   string clean=Trim(value);
   if(clean=="") return false;
   int start=0;
   ushort first=StringGetCharacter(clean,0);
   if(first=='-' || first=='+') start=1;
   if(start>=StringLen(clean)) return false;
   for(int i=start;i<StringLen(clean);i++)
   {
      ushort ch=StringGetCharacter(clean,i);
      if(ch<'0' || ch>'9') return false;
   }
   number=StringToInteger(clean);
   return true;
}

bool LoadCatalog()
{
   ResetLastError();
   int handle=FileOpen(CatalogFile,FILE_READ|FILE_CSV|FILE_ANSI|FILE_SHARE_READ,',');
   if(handle!=INVALID_HANDLE)
      catalog_location="LOCAL_MQL5_FILES\\"+CatalogFile;
   if(handle==INVALID_HANDLE && TrySharedCatalog)
   {
      ResetLastError();
      handle=FileOpen(CatalogFile,FILE_READ|FILE_CSV|FILE_ANSI|FILE_SHARE_READ|FILE_COMMON,',');
      if(handle!=INVALID_HANDLE)
         catalog_location="COMMON_FILES\\"+CatalogFile;
   }
   if(handle==INVALID_HANDLE)
   {
      AddWarning("CATALOGO_AUSENTE: "+CatalogFile+" (error "+IntegerToString(GetLastError())+")");
      return false;
   }

   int line=0;
   while(!FileIsEnding(handle))
   {
      string name=FileReadString(handle);
      if(FileIsEnding(handle) && name=="") break;
      string magic_text=FileReadString(handle);
      string comment=FileReadString(handle);
      line++;
      catalog_lines=line;
      if(line==1 && NormalizeEx5Name(name)=="ex5_name")
         continue;
      name=Trim(name);
      if(name=="" && Trim(magic_text)=="" && Trim(comment)=="")
         continue;
      long magic=0;
      if(name=="" || !ParseLongStrict(magic_text,magic))
      {
         AddWarning("CATALOGO_LINEA_INVALIDA: "+IntegerToString(line));
         continue;
      }
      int size=ArraySize(catalog);
      ArrayResize(catalog,size+1);
      catalog[size].ex5_name=DisplayEx5Name(name);
      catalog[size].normalized_name=NormalizeEx5Name(name);
      catalog[size].magic=magic;
      catalog[size].expected_comment=Trim(comment);
      catalog[size].duplicate=false;
      catalog[size].shared_magic=false;
      catalog_valid_rows++;
   }
   FileClose(handle);

   for(int i=0;i<ArraySize(catalog);i++)
   {
      for(int j=i+1;j<ArraySize(catalog);j++)
      {
         if(catalog[i].normalized_name==catalog[j].normalized_name && catalog[i].magic==catalog[j].magic)
         {
            catalog[i].duplicate=true;
            catalog[j].duplicate=true;
            AddWarning("CATALOGO_DUPLICADO: "+catalog[i].ex5_name+" / MN "+(string)catalog[i].magic);
         }
         if(catalog[i].magic==catalog[j].magic && catalog[i].normalized_name!=catalog[j].normalized_name)
         {
            catalog[i].shared_magic=true;
            catalog[j].shared_magic=true;
            AddWarning("MN_COMPARTIDO: "+(string)catalog[i].magic+" usado por "+catalog[i].ex5_name+" y "+catalog[j].ex5_name);
         }
      }
   }
   if(catalog_valid_rows==0)
      AddWarning("CATALOGO_SIN_FILAS_VALIDAS: "+catalog_location);
   return true;
}

bool CatalogContainsPair(const string ex5,const long magic)
{
   string normalized=NormalizeEx5Name(ex5);
   for(int i=0;i<ArraySize(catalog);i++)
      if(catalog[i].normalized_name==normalized && catalog[i].magic==magic) return true;
   return false;
}

void AddCatalogPairInMemory(const string ex5,const long magic,const string expected_comment)
{
   int size=ArraySize(catalog);
   ArrayResize(catalog,size+1);
   catalog[size].ex5_name=DisplayEx5Name(ex5);
   catalog[size].normalized_name=NormalizeEx5Name(ex5);
   catalog[size].magic=magic;
   catalog[size].expected_comment=Trim(expected_comment);
   catalog[size].duplicate=false;
   catalog[size].shared_magic=false;
   catalog_lines++;
   catalog_valid_rows++;
}

// MQL5 cannot inspect the input parameters of an EA attached to another chart.
// Only persist a unique association recovered from an executed-deal signature.
bool AppendAutoCatalogPair(const string ex5,const long magic,const string expected_comment)
{
   if(ex5=="" || NormalizeEx5Name(ex5)=="" || CatalogContainsPair(ex5,magic)) return false;

   ResetLastError();
   int handle=FileOpen(CatalogFile,FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_SHARE_READ,',');
   if(handle==INVALID_HANDLE)
   {
      AddWarning("CATALOGO_AUTO_NO_ESCRIBIBLE: "+CatalogFile+" (error "+IntegerToString(GetLastError())+")");
      return false;
   }

   if(FileSize(handle)==0)
      FileWrite(handle,"ex5_name","magic_number","expected_comment");
   FileSeek(handle,0,SEEK_END);
   FileWrite(handle,DisplayEx5Name(ex5),(string)magic,Trim(expected_comment));
   FileFlush(handle);
   FileClose(handle);

   AddCatalogPairInMemory(ex5,magic,expected_comment);
   catalog_auto_added++;
   return true;
}

int FindStats(const string symbol,const long magic)
{
   for(int i=0;i<ArraySize(history_stats);i++)
      if(history_stats[i].magic==magic && history_stats[i].symbol==symbol) return i;
   int size=ArraySize(history_stats);
   ArrayResize(history_stats,size+1);
   history_stats[size].symbol=symbol;
   history_stats[size].magic=magic;
   history_stats[size].entries=0;
   history_stats[size].first_trade=0;
   history_stats[size].last_trade=0;
   history_stats[size].deal_comments="";
   history_stats[size].orders=0;
   history_stats[size].first_order=0;
   history_stats[size].last_order=0;
   history_stats[size].order_comments="";
   history_stats[size].order_states="";
   return size;
}

void AddUniqueComment(string &target,const string value)
{
   string clean=Trim(value);
   if(clean=="") return;
   if(StringFind(target,clean)>=0) return;
   if(target!="") target+=" | ";
   target+=clean;
}

bool OrderTicketAlreadySeen(const ulong ticket)
{
   for(int i=0;i<ArraySize(observed_order_tickets);i++)
      if(observed_order_tickets[i]==ticket) return true;
   int size=ArraySize(observed_order_tickets);
   ArrayResize(observed_order_tickets,size+1);
   observed_order_tickets[size]=ticket;
   return false;
}

bool IsTradingOrderType(const ENUM_ORDER_TYPE type)
{
   return (type==ORDER_TYPE_BUY || type==ORDER_TYPE_SELL ||
           type==ORDER_TYPE_BUY_LIMIT || type==ORDER_TYPE_SELL_LIMIT ||
           type==ORDER_TYPE_BUY_STOP || type==ORDER_TYPE_SELL_STOP ||
           type==ORDER_TYPE_BUY_STOP_LIMIT || type==ORDER_TYPE_SELL_STOP_LIMIT);
}

void AddOrderActivity(const ulong ticket,const string symbol,const long magic,const datetime time,
                      const string comment,const ENUM_ORDER_STATE state,const bool current)
{
   if(ticket==0 || OrderTicketAlreadySeen(ticket)) return;
   int index=FindStats(symbol,magic);
   history_stats[index].orders++;
   if(history_stats[index].first_order==0 || time<history_stats[index].first_order)
      history_stats[index].first_order=time;
   if(time>history_stats[index].last_order)
      history_stats[index].last_order=time;
   AddUniqueComment(history_stats[index].order_comments,comment);
   AddUniqueComment(history_stats[index].order_states,current ? "ACTIVA" : EnumToString(state));
   if(current) current_orders_loaded++;
   else historical_orders_loaded++;
}

bool LoadHistory()
{
   bool history_ok=HistorySelect((datetime)0,TimeCurrent());
   if(!history_ok)
   {
      AddWarning("HISTORIAL_NO_DISPONIBLE: error "+IntegerToString(GetLastError()));
   }
   else
   {
      int total=HistoryDealsTotal();
      for(int i=0;i<total;i++)
      {
         ulong ticket=HistoryDealGetTicket(i);
         if(ticket==0) continue;
         ENUM_DEAL_TYPE type=(ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket,DEAL_TYPE);
         ENUM_DEAL_ENTRY entry=(ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket,DEAL_ENTRY);
         if(type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL) continue;
         if(entry!=DEAL_ENTRY_IN && entry!=DEAL_ENTRY_INOUT) continue;
         long magic=HistoryDealGetInteger(ticket,DEAL_MAGIC);
         string symbol=HistoryDealGetString(ticket,DEAL_SYMBOL);
         datetime time=(datetime)HistoryDealGetInteger(ticket,DEAL_TIME);
         int index=FindStats(symbol,magic);
         history_stats[index].entries++;
         if(history_stats[index].first_trade==0 || time<history_stats[index].first_trade)
            history_stats[index].first_trade=time;
         if(time>history_stats[index].last_trade)
            history_stats[index].last_trade=time;
         AddUniqueComment(history_stats[index].deal_comments,HistoryDealGetString(ticket,DEAL_COMMENT));
      }

      int history_orders=HistoryOrdersTotal();
      for(int i=0;i<history_orders;i++)
      {
         ulong ticket=HistoryOrderGetTicket(i);
         if(ticket==0) continue;
         ENUM_ORDER_TYPE type=(ENUM_ORDER_TYPE)HistoryOrderGetInteger(ticket,ORDER_TYPE);
         if(!IsTradingOrderType(type)) continue;
         AddOrderActivity(ticket,HistoryOrderGetString(ticket,ORDER_SYMBOL),
                          HistoryOrderGetInteger(ticket,ORDER_MAGIC),
                          (datetime)HistoryOrderGetInteger(ticket,ORDER_TIME_SETUP),
                          HistoryOrderGetString(ticket,ORDER_COMMENT),
                          (ENUM_ORDER_STATE)HistoryOrderGetInteger(ticket,ORDER_STATE),false);
      }
   }

   int active_orders=OrdersTotal();
   for(int i=0;i<active_orders;i++)
   {
      ulong ticket=OrderGetTicket(i);
      if(ticket==0) continue;
      ENUM_ORDER_TYPE type=(ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(!IsTradingOrderType(type)) continue;
      AddOrderActivity(ticket,OrderGetString(ORDER_SYMBOL),OrderGetInteger(ORDER_MAGIC),
                       (datetime)OrderGetInteger(ORDER_TIME_SETUP),OrderGetString(ORDER_COMMENT),
                       (ENUM_ORDER_STATE)OrderGetInteger(ORDER_STATE),true);
   }
   return history_ok;
}

int ExistingStats(const string symbol,const long magic)
{
   for(int i=0;i<ArraySize(history_stats);i++)
      if(history_stats[i].magic==magic && history_stats[i].symbol==symbol) return i;
   return -1;
}

string StrategySignature(const string value)
{
   // The EA name can end in .ex5.  Remove it before extracting numeric
   // groups, otherwise the "5" in the extension becomes a false component
   // of the signature (for example 4.7.21(2).ex5 -> 4.7.21.2.5).
   string source=NormalizeEx5Name(value);
   if(source=="")
      source=StringToLowerCopy(value);
   int strategy_pos=StringFind(source,"strategy");
   if(strategy_pos>=0)
      source=StringSubstr(source,strategy_pos+8);
   string result="";
   string number="";
   for(int i=0;i<StringLen(source);i++)
   {
      ushort ch=StringGetCharacter(source,i);
      if(ch>='0' && ch<='9')
         number+=ShortToString(ch);
      else if(number!="")
      {
         if(result!="") result+=".";
         result+=number;
         number="";
      }
   }
   if(number!="")
   {
      if(result!="") result+=".";
      result+=number;
   }
   return result;
}

int AutoMatchByDeals(const string symbol,const string ex5,string &confidence,string &reason)
{
   string signature=StrategySignature(ex5);
   confidence="SIN_COINCIDENCIA";
   reason="FIRMA_DEALS="+signature;
   if(signature=="") return -1;
   int found=-1;
   int candidates=0;
   for(int i=0;i<ArraySize(history_stats);i++)
   {
      if(StringToLowerCopy(history_stats[i].symbol)!=StringToLowerCopy(symbol)) continue;
      if(history_stats[i].entries<=0) continue;
      if(StrategySignature(history_stats[i].deal_comments)!=signature) continue;
      found=i;
      candidates++;
   }
   if(candidates==1)
   {
      confidence="PROBABLE_FIRMA_DEAL_Y_SIMBOLO";
      reason="Firma "+signature+" coincide con un único MN del historial";
      return found;
   }
   if(candidates>1)
   {
      confidence="AMBIGUA";
      reason="Firma "+signature+" coincide con "+IntegerToString(candidates)+" MN en deals";
   }
   return -1;
}

int AutoMatchByOrders(const string symbol,const string ex5,string &confidence,string &reason)
{
   string signature=StrategySignature(ex5);
   confidence="SIN_COINCIDENCIA";
   reason="FIRMA_ORDENES="+signature;
   if(signature=="") return -1;
   int found=-1;
   int candidates=0;
   for(int i=0;i<ArraySize(history_stats);i++)
   {
      if(StringToLowerCopy(history_stats[i].symbol)!=StringToLowerCopy(symbol)) continue;
      if(history_stats[i].orders<=0) continue;
      if(StrategySignature(history_stats[i].order_comments)!=signature) continue;
      found=i;
      candidates++;
   }
   if(candidates==1)
   {
      confidence="PROBABLE_FIRMA_ORDEN_Y_SIMBOLO";
      reason="Firma "+signature+" coincide con un unico MN en ordenes";
      return found;
   }
   if(candidates>1)
   {
      confidence="AMBIGUA";
      reason="Firma "+signature+" coincide con "+IntegerToString(candidates)+" MN en ordenes";
   }
   return -1;
}

string ObservedComments(const int stat)
{
   if(stat<0) return "";
   string result=history_stats[stat].deal_comments;
   string orders=history_stats[stat].order_comments;
   if(orders!="" && StringFind(result,orders)<0)
   {
      if(result!="") result+=" | ";
      result+=orders;
   }
   return result;
}

string ActivityStatus(const int stat)
{
   if(stat<0) return "SIN_ACTIVIDAD";
   if(history_stats[stat].entries>0) return "CON_ENTRADAS";
   if(history_stats[stat].orders>0) return "SOLO_ORDENES";
   return "SIN_ACTIVIDAD";
}

string PeriodText(const ENUM_TIMEFRAMES period)
{
   string value=EnumToString(period);
   if(StringFind(value,"PERIOD_")==0) value=StringSubstr(value,7);
   return value;
}

string WarningFlags(const int catalog_index,const string observed_comments)
{
   string flags="";
   if(catalog_index<0) return "MN_DESCONOCIDO";
   if(catalog[catalog_index].duplicate) flags="CATALOGO_DUPLICADO";
   if(catalog[catalog_index].shared_magic)
   {
      if(flags!="") flags+=" | ";
      flags+="MN_COMPARTIDO";
   }
   string expected=catalog[catalog_index].expected_comment;
   if(expected!="" && observed_comments!="" && StringFind(observed_comments,expected)<0)
   {
      if(flags!="") flags+=" | ";
      flags+="COMENTARIO_DIFIERE";
   }
   return flags;
}

void WriteCsvHeader(const int handle)
{
   FileWriteString(handle,"generated_utc,account_login,server,chart_id,symbol,timeframe,ex5_name,magic_number,mapping_source,mapping_confidence,expected_comment,observed_comments,attached,terminal_connected,algo_trading_global,account_trade_allowed,status,trade_status,activity_status,entry_deals,first_trade,last_trade,orders,first_order,last_order,order_states,warnings\r\n");
}

string CsvRow(const string generated,const long account,const string server,const long chart_id,const string symbol,
              const string timeframe,const string ex5,const string magic,const string mapping_source,const string confidence,
              const string expected,const string observed,
              const bool attached,const bool connected,const bool algo,const bool account_allowed,const string status,
              const string trade_status,const string activity_status,const string entries,
              const datetime first_trade,const datetime last_trade,const string orders,const datetime first_order,
              const datetime last_order,const string order_states,const string flags)
{
   return CsvEscape(generated)+","+CsvEscape((string)account)+","+CsvEscape(server)+","+CsvEscape((string)chart_id)+","+
          CsvEscape(symbol)+","+CsvEscape(timeframe)+","+CsvEscape(ex5)+","+CsvEscape(magic)+","+CsvEscape(mapping_source)+","+
          CsvEscape(confidence)+","+CsvEscape(expected)+","+
          CsvEscape(observed)+","+CsvEscape(BoolText(attached))+","+CsvEscape(BoolText(connected))+","+CsvEscape(BoolText(algo))+","+
          CsvEscape(BoolText(account_allowed))+","+CsvEscape(status)+","+CsvEscape(trade_status)+","+CsvEscape(activity_status)+","+
          CsvEscape(entries)+","+CsvEscape(TimeText(first_trade))+","+CsvEscape(TimeText(last_trade))+","+CsvEscape(orders)+","+
          CsvEscape(TimeText(first_order))+","+CsvEscape(TimeText(last_order))+","+CsvEscape(order_states)+","+CsvEscape(flags)+"\r\n";
}

string HtmlRow(const long chart_id,const string symbol,const string timeframe,const string ex5,const string magic,
               const string mapping_source,const string confidence,const string expected,const string observed,
               const string status,const string trade_status,const string activity_status,const string entries,
               const datetime first_trade,const datetime last_trade,const string orders,const datetime first_order,
               const datetime last_order,const string order_states,const string flags)
{
   string css=status=="ACTIVO" ? "active" : (status=="SIN_EA" ? "empty" : "inactive");
   return "<tr class='"+css+"'><td>"+(string)chart_id+"</td><td>"+HtmlEscape(symbol)+"</td><td>"+HtmlEscape(timeframe)+
          "</td><td>"+HtmlEscape(ex5)+"</td><td>"+HtmlEscape(magic)+"</td><td>"+HtmlEscape(mapping_source)+"</td><td>"+
          HtmlEscape(confidence)+"</td><td>"+HtmlEscape(expected)+"</td><td>"+
          HtmlEscape(observed)+"</td><td>"+status+"</td><td>"+trade_status+"</td><td>"+activity_status+"</td><td>"+
          HtmlEscape(entries)+"</td><td>"+HtmlEscape(TimeText(first_trade))+"</td><td>"+HtmlEscape(TimeText(last_trade))+"</td><td>"+
          HtmlEscape(orders)+"</td><td>"+HtmlEscape(TimeText(first_order))+"</td><td>"+HtmlEscape(TimeText(last_order))+"</td><td>"+
          HtmlEscape(order_states)+"</td><td>"+HtmlEscape(flags)+"</td></tr>\r\n";
}

void WriteHistoryCsv(const string path,const string generated,const long account,const string server)
{
   int handle=FileOpen(path,FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(handle==INVALID_HANDLE)
   {
      AddWarning("NO_SE_PUDO_CREAR_HISTORY_CSV: "+path+" error "+IntegerToString(GetLastError()));
      return;
   }
   FileWriteString(handle,"generated_utc,account_login,server,symbol,magic_number,deal_comments,entry_deals,first_trade,last_trade,order_comments,orders,first_order,last_order,order_states,activity_status\r\n");
   for(int i=0;i<ArraySize(history_stats);i++)
   {
      FileWriteString(handle,CsvEscape(generated)+","+CsvEscape((string)account)+","+CsvEscape(server)+","+
                      CsvEscape(history_stats[i].symbol)+","+CsvEscape((string)history_stats[i].magic)+","+
                      CsvEscape(history_stats[i].deal_comments)+","+CsvEscape((string)history_stats[i].entries)+","+
                      CsvEscape(TimeText(history_stats[i].first_trade))+","+CsvEscape(TimeText(history_stats[i].last_trade))+","+
                      CsvEscape(history_stats[i].order_comments)+","+CsvEscape((string)history_stats[i].orders)+","+
                      CsvEscape(TimeText(history_stats[i].first_order))+","+CsvEscape(TimeText(history_stats[i].last_order))+","+
                      CsvEscape(history_stats[i].order_states)+","+CsvEscape(ActivityStatus(i))+"\r\n");
   }
   FileClose(handle);
}

string HistoryHtmlRows()
{
   string rows="";
   for(int i=0;i<ArraySize(history_stats);i++)
   {
      rows+="<tr><td>"+HtmlEscape(history_stats[i].symbol)+"</td><td>"+(string)history_stats[i].magic+"</td><td>"+
            HtmlEscape(history_stats[i].deal_comments)+"</td><td>"+(string)history_stats[i].entries+"</td><td>"+
            HtmlEscape(TimeText(history_stats[i].first_trade))+"</td><td>"+HtmlEscape(TimeText(history_stats[i].last_trade))+"</td><td>"+
            HtmlEscape(history_stats[i].order_comments)+"</td><td>"+(string)history_stats[i].orders+"</td><td>"+
            HtmlEscape(TimeText(history_stats[i].first_order))+"</td><td>"+HtmlEscape(TimeText(history_stats[i].last_order))+"</td><td>"+
            HtmlEscape(history_stats[i].order_states)+"</td><td>"+ActivityStatus(i)+"</td></tr>\r\n";
   }
   if(rows=="")
      rows="<tr><td colspan='11'>No se encontraron entradas ni ordenes en el historial disponible.</td></tr>\r\n";
   return rows;
}

void WriteDiagnosticsCsv(const string path,const string generated,const long account,const string server,
                         const bool catalog_ok,const bool history_ok,const int charts,const int excluded_script_charts,
                         const int auto_rows,const int ambiguous_rows)
{
   int handle=FileOpen(path,FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(handle==INVALID_HANDLE)
      return;
   FileWriteString(handle,"generated_utc,account_login,server,catalog_read,catalog_location,catalog_lines,catalog_valid_rows,catalog_exact_matches,history_read,activity_magic_groups,historical_orders,current_orders,charts_reported,script_charts_excluded,auto_matched_rows,ambiguous_auto_matches,warnings\r\n");
   string all_warnings="";
   for(int i=0;i<ArraySize(warnings);i++)
   {
      if(all_warnings!="") all_warnings+=" | ";
      all_warnings+=warnings[i];
   }
   FileWriteString(handle,CsvEscape(generated)+","+CsvEscape((string)account)+","+CsvEscape(server)+","+
                   CsvEscape(BoolText(catalog_ok))+","+CsvEscape(catalog_location)+","+CsvEscape((string)catalog_lines)+","+
                   CsvEscape((string)catalog_valid_rows)+","+CsvEscape((string)catalog_exact_matches)+","+
                   CsvEscape(BoolText(history_ok))+","+CsvEscape((string)ArraySize(history_stats))+","+
                   CsvEscape((string)historical_orders_loaded)+","+CsvEscape((string)current_orders_loaded)+","+CsvEscape((string)charts)+","+
                   CsvEscape((string)excluded_script_charts)+","+CsvEscape((string)auto_rows)+","+CsvEscape((string)ambiguous_rows)+","+
                   CsvEscape(all_warnings)+"\r\n");
   FileClose(handle);
}

/*
void OnStart()
{
   FolderCreate(OutputFolder);
   bool catalog_ok=LoadCatalog();
   bool history_ok=LoadHistory();
   string generated=IsoUtc();
   long account=AccountInfoInteger(ACCOUNT_LOGIN);
   string server=AccountInfoString(ACCOUNT_SERVER);
   bool connected=(bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   bool algo=(bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED);
   bool account_allowed=(bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) && (bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT);

   string stamp=TimeToString(TimeLocal(),TIME_DATE|TIME_MINUTES|TIME_SECONDS);
   StringReplace(stamp,".",""); StringReplace(stamp,":",""); StringReplace(stamp," ","_");
   string base=OutputFolder+"\\BotInventory_"+(string)account+"_"+stamp;
   int csv=FileOpen(base+".csv",FILE_WRITE|FILE_TXT|FILE_ANSI);
   int html=FileOpen(base+".html",FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(csv==INVALID_HANDLE || html==INVALID_HANDLE)
   {
      Print("BotInventoryReport: no se pudieron crear los reportes. Error=",GetLastError());
      if(csv!=INVALID_HANDLE) FileClose(csv);
      if(html!=INVALID_HANDLE) FileClose(html);
      return;
   }
   WriteCsvHeader(csv);
   string rows="";
   int charts=0,ea_charts=0,active_rows=0,never_rows=0,unknown_rows=0,total_rows=0;
   int entry_rows=0,only_order_rows=0,no_activity_rows=0;
   long chart_id=ChartFirst();
   while(chart_id>=0)
   {
      charts++;
      string symbol=ChartSymbol(chart_id);
      string timeframe=PeriodText(ChartPeriod(chart_id));
      string expert_raw=ChartGetString(chart_id,CHART_EXPERT_NAME);
      bool attached=(expert_raw!="");
      string ex5=attached ? DisplayEx5Name(expert_raw) : "";
      string normalized=NormalizeEx5Name(expert_raw);
      string status=!attached ? "SIN_EA" : ((connected && algo && account_allowed) ? "ACTIVO" : "INACTIVO");
      if(attached) ea_charts++;
      bool matched=false;
      for(int c=0;c<ArraySize(catalog);c++)
      {
         if(!attached || catalog[c].normalized_name!=normalized) continue;
         matched=true;
         int stat=ExistingStats(catalog[c].magic);
         int entries=stat>=0 ? history_stats[stat].entries : 0;
         datetime first_trade=stat>=0 ? history_stats[stat].first_trade : 0;
         datetime last_trade=stat>=0 ? history_stats[stat].last_trade : 0;
         int orders=stat>=0 ? history_stats[stat].orders : 0;
         datetime first_order=stat>=0 ? history_stats[stat].first_order : 0;
         datetime last_order=stat>=0 ? history_stats[stat].last_order : 0;
         string order_states=stat>=0 ? history_stats[stat].order_states : "";
         string observed=ObservedComments(stat);
         string trade_status=history_ok ? (entries==0 ? "NUNCA_OPERO" : "CON_OPERACIONES") : "HISTORIAL_NO_DISPONIBLE";
         string activity_status=ActivityStatus(stat);
         string flags=WarningFlags(c,observed);
         FileWriteString(csv,CsvRow(generated,account,server,chart_id,symbol,timeframe,ex5,(string)catalog[c].magic,
                                    catalog[c].expected_comment,observed,attached,connected,algo,account_allowed,status,
                                    trade_status,entries,first_trade,last_trade,flags));
         rows+=HtmlRow(chart_id,symbol,timeframe,ex5,(string)catalog[c].magic,catalog[c].expected_comment,observed,status,
                       trade_status,entries,first_trade,last_trade,flags);
         total_rows++; if(status=="ACTIVO") active_rows++; if(entries==0 && history_ok) never_rows++;
      }
      if(!matched)
      {
         string trade_status=attached ? "MN_DESCONOCIDO" : "NO_APLICA";
         string flags=attached ? "MN_DESCONOCIDO" : "";
         FileWriteString(csv,CsvRow(generated,account,server,chart_id,symbol,timeframe,ex5,"","","",attached,connected,
                                    algo,account_allowed,status,trade_status,0,0,0,flags));
         rows+=HtmlRow(chart_id,symbol,timeframe,ex5,"","","",status,trade_status,0,0,0,flags);
         total_rows++; if(status=="ACTIVO") active_rows++; if(attached) unknown_rows++;
      }
      chart_id=ChartNext(chart_id);
   }
   FileClose(csv);

   string warning_html="<ul>";
   if(ArraySize(warnings)==0) warning_html+="<li>Sin advertencias.</li>";
   for(int i=0;i<ArraySize(warnings);i++) warning_html+="<li>"+HtmlEscape(warnings[i])+"</li>";
   warning_html+="</ul>";
   string document="<!doctype html><html><head><meta charset='windows-1252'><title>Inventario de bots MT5</title>"
      "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#17202a}h1{margin-bottom:4px}.meta{color:#566573}"
      ".cards{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}.card{padding:12px 18px;border:1px solid #ccd1d1;border-radius:8px}"
      "table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #d5d8dc;padding:7px;text-align:left;vertical-align:top}"
      "th{background:#273746;color:white;position:sticky;top:0}.active{background:#eafaf1}.inactive{background:#fef5e7}.empty{background:#f4f6f7}"
      "</style></head><body><h1>Inventario de bots MT5</h1><div class='meta'>Generado UTC: "+HtmlEscape(generated)+
      " | Cuenta: "+(string)account+" | Servidor: "+HtmlEscape(server)+"</div><div class='cards'>"
      "<div class='card'><b>Charts</b><br>"+(string)charts+"</div><div class='card'><b>Charts con EA</b><br>"+(string)ea_charts+
      "</div><div class='card'><b>Filas activas</b><br>"+(string)active_rows+"</div><div class='card'><b>Nunca operaron</b><br>"+
      (string)never_rows+"</div><div class='card'><b>MN desconocido</b><br>"+(string)unknown_rows+"</div></div>"
      "<p>Conectado: <b>"+BoolText(connected)+"</b> | Algo Trading global: <b>"+BoolText(algo)+
      "</b> | Trading de cuenta/EA permitido: <b>"+BoolText(account_allowed)+"</b> | Catálogo leído: <b>"+BoolText(catalog_ok)+
      "</b> | Historial leído: <b>"+BoolText(history_ok)+"</b></p><h2>Advertencias</h2>"+warning_html+
      "<h2>Detalle</h2><table><thead><tr><th>Chart ID</th><th>Símbolo</th><th>TF</th><th>EX5</th><th>MN</th>"
      "<th>Comentario esperado</th><th>Comentarios observados</th><th>Estado</th><th>Operaciones</th><th>Entradas</th>"
      "<th>Primera</th><th>Última</th><th>Advertencias</th></tr></thead><tbody>"+rows+"</tbody></table>"
      "<p class='meta'>Sólo lectura. NUNCA_OPERO se limita al historial disponible de la cuenta indicada.</p></body></html>";
   FileWriteString(html,document);
   FileClose(html);
   Print("BotInventoryReport: completado. Charts=",charts," filas=",total_rows," CSV=",base,".csv HTML=",base,".html");
}
*/

void OnStart()
{
   FolderCreate(OutputFolder);
   bool catalog_ok=LoadCatalog();
   bool history_ok=LoadHistory();
   string generated=IsoUtc();
   long account=AccountInfoInteger(ACCOUNT_LOGIN);
   string server=AccountInfoString(ACCOUNT_SERVER);
   bool connected=(bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   bool algo=(bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED);
   bool account_allowed=(bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) && (bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT);

   string stamp=TimeToString(TimeLocal(),TIME_DATE|TIME_MINUTES|TIME_SECONDS);
   StringReplace(stamp,".","");
   StringReplace(stamp,":","");
   StringReplace(stamp," ","_");
   string base=OutputFolder+"\\BotInventory_"+(string)account+"_"+stamp;

   int csv=FileOpen(base+".csv",FILE_WRITE|FILE_TXT|FILE_ANSI);
   int html=FileOpen(base+".html",FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(csv==INVALID_HANDLE || html==INVALID_HANDLE)
   {
      Print("BotInventoryReport: no se pudieron crear los reportes. Error=",GetLastError());
      if(csv!=INVALID_HANDLE) FileClose(csv);
      if(html!=INVALID_HANDLE) FileClose(html);
      return;
   }

   WriteCsvHeader(csv);
   string rows="";
   string pending_rows="";
   int charts=0,ea_charts=0,active_rows=0,never_rows=0,unknown_rows=0,total_rows=0;
   int entry_rows=0,only_order_rows=0,no_activity_rows=0;
   int excluded_script_charts=0,auto_rows=0,ambiguous_rows=0;
   long script_chart=ChartID();

   long chart_id=ChartFirst();
   while(chart_id>=0)
   {
      if(chart_id==script_chart)
      {
         excluded_script_charts++;
         chart_id=ChartNext(chart_id);
         continue;
      }

      charts++;
      string symbol=ChartSymbol(chart_id);
      string timeframe=PeriodText(ChartPeriod(chart_id));
      string expert_raw=ChartGetString(chart_id,CHART_EXPERT_NAME);
      bool attached=(Trim(expert_raw)!="");
      string ex5=attached ? DisplayEx5Name(expert_raw) : "";
      string normalized=NormalizeEx5Name(expert_raw);
      string status=!attached ? "SIN_EA" : ((connected && algo && account_allowed) ? "ACTIVO" : "INACTIVO");
      if(attached) ea_charts++;

      bool matched=false;
      for(int c=0;c<ArraySize(catalog);c++)
      {
         if(!attached || catalog[c].normalized_name!=normalized) continue;
         matched=true;
         catalog_exact_matches++;

         int stat=ExistingStats(symbol,catalog[c].magic);
         int entries=stat>=0 ? history_stats[stat].entries : 0;
         datetime first_trade=stat>=0 ? history_stats[stat].first_trade : 0;
         datetime last_trade=stat>=0 ? history_stats[stat].last_trade : 0;
         int orders=stat>=0 ? history_stats[stat].orders : 0;
         datetime first_order=stat>=0 ? history_stats[stat].first_order : 0;
         datetime last_order=stat>=0 ? history_stats[stat].last_order : 0;
         string order_states=stat>=0 ? history_stats[stat].order_states : "";
         string observed=ObservedComments(stat);
         string trade_status=history_ok ? (entries==0 ? "NUNCA_OPERO" : "CON_OPERACIONES") : "HISTORIAL_NO_DISPONIBLE";
         string activity_status=ActivityStatus(stat);
         string flags=WarningFlags(c,observed);

         FileWriteString(csv,CsvRow(generated,account,server,chart_id,symbol,timeframe,ex5,(string)catalog[c].magic,
                                    "CATALOGO","CONFIRMADA_CATALOGO",catalog[c].expected_comment,observed,attached,connected,
                                    algo,account_allowed,status,trade_status,activity_status,(string)entries,first_trade,last_trade,
                                    (string)orders,first_order,last_order,order_states,flags));
         rows+=HtmlRow(chart_id,symbol,timeframe,ex5,(string)catalog[c].magic,"CATALOGO","CONFIRMADA_CATALOGO",
                       catalog[c].expected_comment,observed,status,trade_status,activity_status,(string)entries,first_trade,last_trade,
                       (string)orders,first_order,last_order,order_states,flags);
         total_rows++;
         if(status=="ACTIVO") active_rows++;
         if(entries==0 && history_ok) never_rows++;
         if(activity_status=="CON_ENTRADAS") entry_rows++;
         else if(activity_status=="SOLO_ORDENES") only_order_rows++;
         else if(activity_status=="SIN_ACTIVIDAD") no_activity_rows++;
      }

      if(!matched)
      {
         string confidence="";
         string reason="";
         int stat=attached ? AutoMatchByDeals(symbol,ex5,confidence,reason) : -1;
         string mapping_source="HISTORIAL_DEALS_AUTO";
         if(attached && stat<0)
         {
            string deal_confidence=confidence;
            string deal_reason=reason;
            string order_confidence="";
            string order_reason="";
            int order_stat=AutoMatchByOrders(symbol,ex5,order_confidence,order_reason);
            if(order_stat>=0)
            {
               stat=order_stat;
               confidence=order_confidence;
               reason=order_reason;
               mapping_source="HISTORIAL_ORDENES_AUTO";
            }
            else if(deal_confidence=="AMBIGUA")
            {
               confidence=deal_confidence;
               reason=deal_reason+" | "+order_reason;
            }
            else
            {
               confidence=order_confidence;
               reason=order_reason;
            }
         }

         if(attached && stat>=0)
         {
            string flags="ASOCIACION_AUTOMATICA_REVISAR";
            string entries=(string)history_stats[stat].entries;
            string orders=(string)history_stats[stat].orders;
            string observed=ObservedComments(stat);
            string trade_status=history_ok ? (history_stats[stat].entries==0 ? "NUNCA_OPERO" : "CON_OPERACIONES") : "HISTORIAL_NO_DISPONIBLE";
            string activity_status=ActivityStatus(stat);
            FileWriteString(csv,CsvRow(generated,account,server,chart_id,symbol,timeframe,ex5,(string)history_stats[stat].magic,
                                       mapping_source,confidence,"",observed,attached,connected,algo,account_allowed,status,
                                       trade_status,activity_status,entries,history_stats[stat].first_trade,history_stats[stat].last_trade,
                                       orders,history_stats[stat].first_order,history_stats[stat].last_order,
                                       history_stats[stat].order_states,flags));
            rows+=HtmlRow(chart_id,symbol,timeframe,ex5,(string)history_stats[stat].magic,mapping_source,confidence,"",
                          observed,status,trade_status,activity_status,entries,history_stats[stat].first_trade,
                          history_stats[stat].last_trade,orders,history_stats[stat].first_order,history_stats[stat].last_order,
                          history_stats[stat].order_states,flags);
            pending_rows+=CsvEscape(ex5)+","+CsvEscape((string)history_stats[stat].magic)+","+
                          CsvEscape(observed)+","+CsvEscape(symbol)+","+CsvEscape(confidence)+","+
                          CsvEscape(reason)+"\r\n";
            if(AutoSeedCatalogFromHistory && mapping_source=="HISTORIAL_DEALS_AUTO")
            {
               string catalog_comment=StrategySignature(ex5);
               if(AppendAutoCatalogPair(ex5,history_stats[stat].magic,catalog_comment))
                  AddWarning("CATALOGO_AUTO_AGREGADO: "+ex5+" / MN "+(string)history_stats[stat].magic);
            }
            auto_rows++;
            total_rows++;
            if(status=="ACTIVO") active_rows++;
            if(history_stats[stat].entries==0 && history_ok) never_rows++;
            if(activity_status=="CON_ENTRADAS") entry_rows++;
            else if(activity_status=="SOLO_ORDENES") only_order_rows++;
            else if(activity_status=="SIN_ACTIVIDAD") no_activity_rows++;
         }
         else
         {
            string trade_status=attached ? "MN_DESCONOCIDO" : "NO_APLICA";
            string flags=attached ? "MN_DESCONOCIDO" : "";
            if(attached && confidence=="AMBIGUA")
            {
               flags+=" | ASOCIACION_AUTOMATICA_AMBIGUA";
               ambiguous_rows++;
            }
            string entries_text=attached ? "NO_DETERMINABLE" : "NO_APLICA";
            FileWriteString(csv,CsvRow(generated,account,server,chart_id,symbol,timeframe,ex5,"",
                                       attached ? "SIN_COINCIDENCIA" : "NO_APLICA",confidence,"","",attached,connected,
                                       algo,account_allowed,status,trade_status,attached ? "NO_DETERMINABLE" : "NO_APLICA",
                                       entries_text,0,0,attached ? "NO_DETERMINABLE" : "NO_APLICA",0,0,"",flags));
            rows+=HtmlRow(chart_id,symbol,timeframe,ex5,"",attached ? "SIN_COINCIDENCIA" : "NO_APLICA",confidence,"","",
                          status,trade_status,attached ? "NO_DETERMINABLE" : "NO_APLICA",entries_text,0,0,
                          attached ? "NO_DETERMINABLE" : "NO_APLICA",0,0,"",flags);
            if(attached)
               pending_rows+=CsvEscape(ex5)+",,,"+CsvEscape(symbol)+","+CsvEscape(confidence)+","+CsvEscape(reason)+"\r\n";
            total_rows++;
            if(status=="ACTIVO") active_rows++;
            if(attached) unknown_rows++;
         }
      }
      chart_id=ChartNext(chart_id);
   }
   FileClose(csv);

   if(catalog_ok && catalog_valid_rows>0 && catalog_exact_matches==0)
      AddWarning("CATALOGO_LEIDO_SIN_COINCIDENCIAS_EXACTAS: revisar nombres EX5 o usar sugerencias del historial");
   if(excluded_script_charts>0)
      AddWarning("CHART_DEL_SCRIPT_EXCLUIDO: "+(string)excluded_script_charts);
   if(catalog_auto_added>0)
      AddWarning("CATALOGO_AUTO_AGREGADOS_TOTAL: "+(string)catalog_auto_added);

   string history_path=base+"_History.csv";
   string diagnostics_path=base+"_Diagnostics.csv";
   string pending_path=base+"_CatalogPending.csv";
   WriteHistoryCsv(history_path,generated,account,server);

   int pending=FileOpen(pending_path,FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(pending!=INVALID_HANDLE)
   {
      FileWriteString(pending,"ex5_name,magic_number,expected_comment,symbol,confidence,reason\r\n");
      FileWriteString(pending,pending_rows);
      FileClose(pending);
   }
   WriteDiagnosticsCsv(diagnostics_path,generated,account,server,catalog_ok,history_ok,charts,excluded_script_charts,auto_rows,ambiguous_rows);

   string warning_html="<ul>";
   if(ArraySize(warnings)==0) warning_html+="<li>Sin advertencias.</li>";
   for(int i=0;i<ArraySize(warnings);i++)
      warning_html+="<li>"+HtmlEscape(warnings[i])+"</li>";
   warning_html+="</ul>";

   string diagnostics="<p>Conectado: <b>"+BoolText(connected)+"</b> | Algo Trading global: <b>"+BoolText(algo)+
      "</b> | Trading de cuenta/EA permitido: <b>"+BoolText(account_allowed)+"</b> | Catalogo leido: <b>"+BoolText(catalog_ok)+
      "</b> | Historial leido: <b>"+BoolText(history_ok)+"</b> | Catalogo: <b>"+HtmlEscape(catalog_location)+
      "</b> | Filas validas catalogo: <b>"+(string)catalog_valid_rows+"</b> | Coincidencias exactas: <b>"+
      (string)catalog_exact_matches+"</b> | Ordenes historicas: <b>"+(string)historical_orders_loaded+
      "</b> | Ordenes actuales: <b>"+(string)current_orders_loaded+"</b></p>";

   string document="<!doctype html><html><head><meta charset='windows-1252'><title>Inventario de bots MT5</title>"
      "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#17202a}h1{margin-bottom:4px}.meta{color:#566573}"
      ".cards{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}.card{padding:12px 18px;border:1px solid #ccd1d1;border-radius:8px}"
      "table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #d5d8dc;padding:7px;text-align:left;vertical-align:top}"
      "th{background:#273746;color:white;position:sticky;top:0}.active{background:#eafaf1}.inactive{background:#fef5e7}.empty{background:#f4f6f7}"
      "</style></head><body><h1>Inventario de bots MT5</h1><div class='meta'>Generado UTC: "+HtmlEscape(generated)+
      " | Cuenta: "+(string)account+" | Servidor: "+HtmlEscape(server)+"</div><div class='cards'>"
      "<div class='card'><b>Charts</b><br>"+(string)charts+"</div><div class='card'><b>Charts con EA</b><br>"+(string)ea_charts+
      "</div><div class='card'><b>Filas activas</b><br>"+(string)active_rows+"</div><div class='card'><b>Nunca operaron</b><br>"+
      (string)never_rows+"</div><div class='card'><b>Con entradas</b><br>"+(string)entry_rows+
      "</div><div class='card'><b>Solo ordenes</b><br>"+(string)only_order_rows+
      "</div><div class='card'><b>Sin actividad</b><br>"+(string)no_activity_rows+
      "</div><div class='card'><b>MN desconocido</b><br>"+(string)unknown_rows+"</div><div class='card'><b>Auto-asociados</b><br>"+
      (string)auto_rows+"</div></div>"+diagnostics+"<h2>Advertencias</h2>"+warning_html+
      "<h2>Detalle</h2><table><thead><tr><th>Chart ID</th><th>Simbolo</th><th>TF</th><th>EX5</th><th>MN</th>"
      "<th>Origen</th><th>Confianza</th><th>Comentario esperado</th><th>Comentarios observados</th><th>Estado</th><th>Operaciones</th>"
      "<th>Actividad</th><th>Entradas</th><th>Primera entrada</th><th>Ultima entrada</th><th>Ordenes</th>"
      "<th>Primera orden</th><th>Ultima orden</th><th>Estados orden</th><th>Advertencias</th></tr></thead><tbody>"+rows+"</tbody></table>"
      "<h2>Actividad agrupada</h2><table><thead><tr><th>Simbolo</th><th>MN</th><th>Comentarios deals</th><th>Entradas</th>"
      "<th>Primera entrada</th><th>Ultima entrada</th><th>Comentarios ordenes</th><th>Ordenes</th><th>Primera orden</th>"
      "<th>Ultima orden</th><th>Estados</th><th>Actividad</th></tr></thead><tbody>"+HistoryHtmlRows()+"</tbody></table>"
      "<p class='meta'>Solo lectura. NUNCA_OPERO significa cero entradas BUY/SELL ejecutadas y se limita al historial disponible.</p></body></html>";
   FileWriteString(html,document);
   FileClose(html);
   Print("BotInventoryReport: completado. Charts=",charts," filas=",total_rows," CSV=",base,".csv HTML=",base,".html");
}
