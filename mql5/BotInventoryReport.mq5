//+------------------------------------------------------------------+
//| BotInventoryReport.mq5                                           |
//| Read-only inventory of EAs attached to charts in the active      |
//| MetaTrader 5 profile. It never sends or modifies trading orders. |
//+------------------------------------------------------------------+
#property copyright "Dashboardv1"
#property version   "2.00"
#property script_show_inputs

input string CatalogFile = "BotInventoryReport\\BotInventoryCatalog.csv";
input string OutputFolder = "BotInventoryReport";
input bool   TrySharedCatalog = true;

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
   string   comments;
};

CatalogRow catalog[];
MagicStats history_stats[];
string warnings[];
string catalog_location="NO_ENCONTRADO";
int catalog_lines=0;
int catalog_valid_rows=0;
int catalog_exact_matches=0;

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
   history_stats[size].comments="";
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

bool LoadHistory()
{
   if(!HistorySelect((datetime)0,TimeCurrent()))
   {
      AddWarning("HISTORIAL_NO_DISPONIBLE: error "+IntegerToString(GetLastError()));
      return false;
   }
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
      AddUniqueComment(history_stats[index].comments,HistoryDealGetString(ticket,DEAL_COMMENT));
   }
   return true;
}

int ExistingStats(const string symbol,const long magic)
{
   for(int i=0;i<ArraySize(history_stats);i++)
      if(history_stats[i].magic==magic && history_stats[i].symbol==symbol) return i;
   return -1;
}

string StrategySignature(const string value)
{
   string source=StringToLowerCopy(value);
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

int AutoMatchHistory(const string symbol,const string ex5,string &confidence,string &reason)
{
   string signature=StrategySignature(ex5);
   confidence="SIN_COINCIDENCIA";
   reason="FIRMA="+signature;
   if(signature=="") return -1;
   int found=-1;
   int candidates=0;
   for(int i=0;i<ArraySize(history_stats);i++)
   {
      if(StringToLowerCopy(history_stats[i].symbol)!=StringToLowerCopy(symbol)) continue;
      if(StrategySignature(history_stats[i].comments)!=signature) continue;
      found=i;
      candidates++;
   }
   if(candidates==1)
   {
      confidence="PROBABLE_FIRMA_Y_SIMBOLO";
      reason="Firma "+signature+" coincide con un único MN del historial";
      return found;
   }
   if(candidates>1)
   {
      confidence="AMBIGUA";
      reason="Firma "+signature+" coincide con "+IntegerToString(candidates)+" MN";
   }
   return -1;
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
   FileWriteString(handle,"generated_utc,account_login,server,chart_id,symbol,timeframe,ex5_name,magic_number,mapping_source,mapping_confidence,expected_comment,observed_comments,attached,terminal_connected,algo_trading_global,account_trade_allowed,status,trade_status,entry_deals,first_trade,last_trade,warnings\r\n");
}

string CsvRow(const string generated,const long account,const string server,const long chart_id,const string symbol,
              const string timeframe,const string ex5,const string magic,const string mapping_source,const string confidence,
              const string expected,const string observed,
              const bool attached,const bool connected,const bool algo,const bool account_allowed,const string status,
              const string trade_status,const string entries,const datetime first_trade,const datetime last_trade,const string flags)
{
   return CsvEscape(generated)+","+CsvEscape((string)account)+","+CsvEscape(server)+","+CsvEscape((string)chart_id)+","+
          CsvEscape(symbol)+","+CsvEscape(timeframe)+","+CsvEscape(ex5)+","+CsvEscape(magic)+","+CsvEscape(mapping_source)+","+
          CsvEscape(confidence)+","+CsvEscape(expected)+","+
          CsvEscape(observed)+","+CsvEscape(BoolText(attached))+","+CsvEscape(BoolText(connected))+","+CsvEscape(BoolText(algo))+","+
          CsvEscape(BoolText(account_allowed))+","+CsvEscape(status)+","+CsvEscape(trade_status)+","+CsvEscape(entries)+","+
          CsvEscape(TimeText(first_trade))+","+CsvEscape(TimeText(last_trade))+","+CsvEscape(flags)+"\r\n";
}

string HtmlRow(const long chart_id,const string symbol,const string timeframe,const string ex5,const string magic,
               const string mapping_source,const string confidence,const string expected,const string observed,
               const string status,const string trade_status,const string entries,
               const datetime first_trade,const datetime last_trade,const string flags)
{
   string css=status=="ACTIVO" ? "active" : (status=="SIN_EA" ? "empty" : "inactive");
   return "<tr class='"+css+"'><td>"+(string)chart_id+"</td><td>"+HtmlEscape(symbol)+"</td><td>"+HtmlEscape(timeframe)+
          "</td><td>"+HtmlEscape(ex5)+"</td><td>"+HtmlEscape(magic)+"</td><td>"+HtmlEscape(mapping_source)+"</td><td>"+
          HtmlEscape(confidence)+"</td><td>"+HtmlEscape(expected)+"</td><td>"+
          HtmlEscape(observed)+"</td><td>"+status+"</td><td>"+trade_status+"</td><td>"+HtmlEscape(entries)+"</td><td>"+
          HtmlEscape(TimeText(first_trade))+"</td><td>"+HtmlEscape(TimeText(last_trade))+"</td><td>"+HtmlEscape(flags)+"</td></tr>\r\n";
}

void WriteHistoryCsv(const string path,const string generated,const long account,const string server)
{
   int handle=FileOpen(path,FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(handle==INVALID_HANDLE)
   {
      AddWarning("NO_SE_PUDO_CREAR_HISTORY_CSV: "+path+" error "+IntegerToString(GetLastError()));
      return;
   }
   FileWriteString(handle,"generated_utc,account_login,server,symbol,magic_number,observed_comments,entry_deals,first_trade,last_trade\r\n");
   for(int i=0;i<ArraySize(history_stats);i++)
   {
      FileWriteString(handle,CsvEscape(generated)+","+CsvEscape((string)account)+","+CsvEscape(server)+","+
                      CsvEscape(history_stats[i].symbol)+","+CsvEscape((string)history_stats[i].magic)+","+
                      CsvEscape(history_stats[i].comments)+","+CsvEscape((string)history_stats[i].entries)+","+
                      CsvEscape(TimeText(history_stats[i].first_trade))+","+CsvEscape(TimeText(history_stats[i].last_trade))+"\r\n");
   }
   FileClose(handle);
}

string HistoryHtmlRows()
{
   string rows="";
   for(int i=0;i<ArraySize(history_stats);i++)
   {
      rows+="<tr><td>"+HtmlEscape(history_stats[i].symbol)+"</td><td>"+(string)history_stats[i].magic+"</td><td>"+
            HtmlEscape(history_stats[i].comments)+"</td><td>"+(string)history_stats[i].entries+"</td><td>"+
            HtmlEscape(TimeText(history_stats[i].first_trade))+"</td><td>"+HtmlEscape(TimeText(history_stats[i].last_trade))+"</td></tr>\r\n";
   }
   if(rows=="")
      rows="<tr><td colspan='6'>No se encontraron entradas BUY/SELL en el historial disponible.</td></tr>\r\n";
   return rows;
}

void WriteDiagnosticsCsv(const string path,const string generated,const long account,const string server,
                         const bool catalog_ok,const bool history_ok,const int charts,const int excluded_script_charts,
                         const int auto_rows,const int ambiguous_rows)
{
   int handle=FileOpen(path,FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(handle==INVALID_HANDLE)
      return;
   FileWriteString(handle,"generated_utc,account_login,server,catalog_read,catalog_location,catalog_lines,catalog_valid_rows,catalog_exact_matches,history_read,history_magic_groups,charts_reported,script_charts_excluded,auto_matched_rows,ambiguous_auto_matches,warnings\r\n");
   string all_warnings="";
   for(int i=0;i<ArraySize(warnings);i++)
   {
      if(all_warnings!="") all_warnings+=" | ";
      all_warnings+=warnings[i];
   }
   FileWriteString(handle,CsvEscape(generated)+","+CsvEscape((string)account)+","+CsvEscape(server)+","+
                   CsvEscape(BoolText(catalog_ok))+","+CsvEscape(catalog_location)+","+CsvEscape((string)catalog_lines)+","+
                   CsvEscape((string)catalog_valid_rows)+","+CsvEscape((string)catalog_exact_matches)+","+
                   CsvEscape(BoolText(history_ok))+","+CsvEscape((string)ArraySize(history_stats))+","+CsvEscape((string)charts)+","+
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
         string observed=stat>=0 ? history_stats[stat].comments : "";
         string trade_status=history_ok ? (entries==0 ? "NUNCA_OPERO" : "CON_OPERACIONES") : "HISTORIAL_NO_DISPONIBLE";
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
         string observed=stat>=0 ? history_stats[stat].comments : "";
         string trade_status=history_ok ? (entries==0 ? "NUNCA_OPERO" : "CON_OPERACIONES") : "HISTORIAL_NO_DISPONIBLE";
         string flags=WarningFlags(c,observed);

         FileWriteString(csv,CsvRow(generated,account,server,chart_id,symbol,timeframe,ex5,(string)catalog[c].magic,
                                    "CATALOGO","CONFIRMADA_CATALOGO",catalog[c].expected_comment,observed,attached,connected,
                                    algo,account_allowed,status,trade_status,(string)entries,first_trade,last_trade,flags));
         rows+=HtmlRow(chart_id,symbol,timeframe,ex5,(string)catalog[c].magic,"CATALOGO","CONFIRMADA_CATALOGO",
                       catalog[c].expected_comment,observed,status,trade_status,(string)entries,first_trade,last_trade,flags);
         total_rows++;
         if(status=="ACTIVO") active_rows++;
         if(entries==0 && history_ok) never_rows++;
      }

      if(!matched)
      {
         string confidence="";
         string reason="";
         int stat=attached ? AutoMatchHistory(symbol,ex5,confidence,reason) : -1;

         if(attached && stat>=0)
         {
            string flags="ASOCIACION_AUTOMATICA_REVISAR";
            string entries=(string)history_stats[stat].entries;
            FileWriteString(csv,CsvRow(generated,account,server,chart_id,symbol,timeframe,ex5,(string)history_stats[stat].magic,
                                       "HISTORIAL_AUTO",confidence,"",history_stats[stat].comments,attached,connected,algo,
                                       account_allowed,status,"CON_OPERACIONES",entries,history_stats[stat].first_trade,
                                       history_stats[stat].last_trade,flags));
            rows+=HtmlRow(chart_id,symbol,timeframe,ex5,(string)history_stats[stat].magic,"HISTORIAL_AUTO",confidence,"",
                          history_stats[stat].comments,status,"CON_OPERACIONES",entries,history_stats[stat].first_trade,
                          history_stats[stat].last_trade,flags);
            pending_rows+=CsvEscape(ex5)+","+CsvEscape((string)history_stats[stat].magic)+","+
                          CsvEscape(history_stats[stat].comments)+","+CsvEscape(symbol)+","+CsvEscape(confidence)+","+
                          CsvEscape(reason)+"\r\n";
            auto_rows++;
            total_rows++;
            if(status=="ACTIVO") active_rows++;
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
                                       algo,account_allowed,status,trade_status,entries_text,0,0,flags));
            rows+=HtmlRow(chart_id,symbol,timeframe,ex5,"",attached ? "SIN_COINCIDENCIA" : "NO_APLICA",confidence,"","",
                          status,trade_status,entries_text,0,0,flags);
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
      (string)catalog_exact_matches+"</b></p>";

   string document="<!doctype html><html><head><meta charset='windows-1252'><title>Inventario de bots MT5</title>"
      "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#17202a}h1{margin-bottom:4px}.meta{color:#566573}"
      ".cards{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}.card{padding:12px 18px;border:1px solid #ccd1d1;border-radius:8px}"
      "table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #d5d8dc;padding:7px;text-align:left;vertical-align:top}"
      "th{background:#273746;color:white;position:sticky;top:0}.active{background:#eafaf1}.inactive{background:#fef5e7}.empty{background:#f4f6f7}"
      "</style></head><body><h1>Inventario de bots MT5</h1><div class='meta'>Generado UTC: "+HtmlEscape(generated)+
      " | Cuenta: "+(string)account+" | Servidor: "+HtmlEscape(server)+"</div><div class='cards'>"
      "<div class='card'><b>Charts</b><br>"+(string)charts+"</div><div class='card'><b>Charts con EA</b><br>"+(string)ea_charts+
      "</div><div class='card'><b>Filas activas</b><br>"+(string)active_rows+"</div><div class='card'><b>Nunca operaron</b><br>"+
      (string)never_rows+"</div><div class='card'><b>MN desconocido</b><br>"+(string)unknown_rows+"</div><div class='card'><b>Auto-asociados</b><br>"+
      (string)auto_rows+"</div></div>"+diagnostics+"<h2>Advertencias</h2>"+warning_html+
      "<h2>Detalle</h2><table><thead><tr><th>Chart ID</th><th>Simbolo</th><th>TF</th><th>EX5</th><th>MN</th>"
      "<th>Origen</th><th>Confianza</th><th>Comentario esperado</th><th>Comentarios observados</th><th>Estado</th><th>Operaciones</th><th>Entradas</th>"
      "<th>Primera</th><th>Ultima</th><th>Advertencias</th></tr></thead><tbody>"+rows+"</tbody></table>"
      "<h2>Historial agrupado</h2><table><thead><tr><th>Simbolo</th><th>MN</th><th>Comentarios observados</th><th>Entradas</th>"
      "<th>Primera</th><th>Ultima</th></tr></thead><tbody>"+HistoryHtmlRows()+"</tbody></table>"
      "<p class='meta'>Solo lectura. NUNCA_OPERO se limita al historial disponible de la cuenta indicada.</p></body></html>";
   FileWriteString(html,document);
   FileClose(html);
   Print("BotInventoryReport: completado. Charts=",charts," filas=",total_rows," CSV=",base,".csv HTML=",base,".html");
}
