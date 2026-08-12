import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const source = "D:/02_BotsTrading/porAI/gestion de escalado/analisis_datos/temp/ReportHistory-4000094894.xlsx";
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(source));

const sheets = await workbook.inspect({
  kind: "sheet",
  include: "id,name",
  maxChars: 12000,
});
console.log("SHEETS");
console.log(sheets.ndjson);

for (const sheet of workbook.worksheets.items) {
  const used = sheet.getUsedRange(true);
  console.log(`USED\t${sheet.name}\t${used?.address ?? ""}`);
  if (!used) continue;
  const preview = await workbook.inspect({
    kind: "table",
    sheetId: sheet.name,
    range: used.address,
    include: "values,formulas",
    tableMaxRows: 15,
    tableMaxCols: 25,
    tableMaxCellChars: 120,
    maxChars: 30000,
  });
  console.log(`TABLE\t${sheet.name}`);
  console.log(preview.ndjson);

  const values = used.values;
  const needles = [
    "AltisimaPrecision", "ORB", "MomentA BuyStop", "VWAP", "Keltnerv2_XAU",
    "ST+RSI", "PPM", "WF_Matrix_NAQStrategy_3_14_14d",
  ];
  console.log(`SECTIONS\t${sheet.name}`);
  for (let i = 0; i < values.length; i++) {
    const rowText = values[i].map(v => String(v ?? "")).join(" | ");
    if (/^(Posiciones|Operaciones|Órdenes|Ordenes|Resultados|Balance|Total)/i.test(String(values[i][0] ?? ""))) {
      console.log(`${i + 1}\t${rowText}`);
    }
  }
  console.log(`MATCHES\t${sheet.name}`);
  for (let i = 0; i < values.length; i++) {
    const rowText = values[i].map(v => String(v ?? "")).join(" | ");
    if (needles.some(n => rowText.toLowerCase().includes(n.toLowerCase()))) {
      console.log(`${i + 1}\t${rowText}`);
    }
  }
}
