//+------------------------------------------------------------------+
//| SymbolSpecProbe.mq5                                              |
//| Read-only MT5 script: writes contract specifications to JSON.    |
//| It never sends, changes, closes or cancels trading orders.       |
//+------------------------------------------------------------------+
#property copyright "Dashboardv1"
#property version   "1.00"
#property script_show_inputs

input string ConfigFile  = "ContractRiskAudit\\SymbolSpecProbeConfig.csv";
input string OutputFolder= "ContractRiskAudit";

string Trim(const string value)
{
   string result=value;
   StringTrimLeft(result); StringTrimRight(result);
   return result;
}

string JsonEscape(string value)
{
   StringReplace(value,"\\","\\\\");
   StringReplace(value,"\"","\\\"");
   StringReplace(value,"\r","\\r");
   StringReplace(value,"\n","\\n");
   return value;
}

string JsonString(const string value) { return "\""+JsonEscape(value)+"\""; }

string IsoUtc()
{
   MqlDateTime p={}; TimeToStruct(TimeGMT(),p);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",p.year,p.mon,p.day,p.hour,p.min,p.sec);
}

string NumberJson(const double value) { return DoubleToString(value,10); }

string SpecJson(const string source,const string symbol)
{
   ResetLastError();
   bool selected=SymbolSelect(symbol,true);
   int error=GetLastError();
   bool available=selected && SymbolInfoInteger(symbol,SYMBOL_EXIST);
   if(!available)
      return "{\"symbol\":"+JsonString(symbol)+",\"available\":false,\"error\":"+
             JsonString("SymbolSelect/SYMBOL_EXIST error "+IntegerToString(error))+"}";

   long digits=SymbolInfoInteger(symbol,SYMBOL_DIGITS);
   long calc_mode=SymbolInfoInteger(symbol,SYMBOL_TRADE_CALC_MODE);
   string result="{\"symbol\":"+JsonString(symbol)+",\"available\":true"+
      ",\"digits\":"+IntegerToString((int)digits)+
      ",\"point\":"+NumberJson(SymbolInfoDouble(symbol,SYMBOL_POINT))+
      ",\"tick_size\":"+NumberJson(SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_SIZE))+
      ",\"tick_value\":"+NumberJson(SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE))+
      ",\"tick_value_profit\":"+NumberJson(SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE_PROFIT))+
      ",\"tick_value_loss\":"+NumberJson(SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE_LOSS))+
      ",\"contract_size\":"+NumberJson(SymbolInfoDouble(symbol,SYMBOL_TRADE_CONTRACT_SIZE))+
      ",\"volume_min\":"+NumberJson(SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN))+
      ",\"volume_max\":"+NumberJson(SymbolInfoDouble(symbol,SYMBOL_VOLUME_MAX))+
      ",\"volume_step\":"+NumberJson(SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP))+
      ",\"volume_limit\":"+NumberJson(SymbolInfoDouble(symbol,SYMBOL_VOLUME_LIMIT))+
      ",\"trade_calc_mode\":"+JsonString(EnumToString((ENUM_SYMBOL_CALC_MODE)calc_mode))+
      ",\"currency_profit\":"+JsonString(SymbolInfoString(symbol,SYMBOL_CURRENCY_PROFIT))+
      ",\"currency_margin\":"+JsonString(SymbolInfoString(symbol,SYMBOL_CURRENCY_MARGIN))+"}";
   return result;
}

void OnStart()
{
   int config=FileOpen(ConfigFile,FILE_READ|FILE_CSV|FILE_ANSI|FILE_SHARE_READ,',');
   if(config==INVALID_HANDLE)
   {
      Print("SymbolSpecProbe: config not found: ",ConfigFile," error=",GetLastError());
      return;
   }
   string source="";
   string rows="";
   int count=0;
   while(!FileIsEnding(config))
   {
      string row_source=Trim(FileReadString(config));
      string symbol=Trim(FileReadString(config));
      if(row_source=="" && symbol=="") continue;
      string folded_source=row_source,folded_symbol=symbol;
      StringToLower(folded_source); StringToLower(folded_symbol);
      if(folded_source=="source" && folded_symbol=="symbol") continue;
      if(source=="") source=row_source;
      if(row_source!=source || symbol=="") continue;
      if(count>0) rows+=",";
      rows+=SpecJson(source,symbol); count++;
   }
   FileClose(config);
   if(source=="") source="unknown";
   FolderCreate(OutputFolder);
   string path=OutputFolder+"\\symbol_specs_"+source+".json";
   int output=FileOpen(path,FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(output==INVALID_HANDLE)
   {
      Print("SymbolSpecProbe: cannot write ",path," error=",GetLastError());
      return;
   }
   string connected=(bool)TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false";
   string json="{\"schema_version\":1,\"source\":"+JsonString(source)+
      ",\"generated_at\":"+JsonString(IsoUtc())+
      ",\"account_login\":"+IntegerToString((int)AccountInfoInteger(ACCOUNT_LOGIN))+
      ",\"server\":"+JsonString(AccountInfoString(ACCOUNT_SERVER))+
      ",\"terminal_connected\":"+connected+
      ",\"symbols\":["+rows+"]}";
   FileWriteString(output,json); FileClose(output);
   Print("SymbolSpecProbe: wrote ",count," symbols to ",path,". Read-only: no trading functions were called.");
}
