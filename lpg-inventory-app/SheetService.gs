/**
 * スプレッドシートへのアクセスをまとめたモジュール。
 * シートの列構成はここで一元管理する。
 *
 * データモデル: 屋外の置場(①〜㉛ 等)ごとに、容器サイズ(20kg/30kg/50kg)別の
 * 実績数(現在の在庫数)とMAX収容数を持つ「容量管理表」。
 * 実物の管理Excel(容器置場可能数)の構成に合わせている。
 */

var SHEET_NAME = '置場容量';

var SIZES = [
  { key: '20', label: '20kg' },
  { key: '30', label: '30kg' },
  { key: '50', label: '50kg' }
];

// スプレッドシート上の列順とプロパティ名の対応。
var COLUMNS = [
  { key: 'no', label: '番号' },
  { key: 'name', label: '置場名' },
  { key: 'position', label: '位置' },
  { key: 'a20', label: '20kg_実績' },
  { key: 'm20', label: '20kg_MAX' },
  { key: 'a30', label: '30kg_実績' },
  { key: 'm30', label: '30kg_MAX' },
  { key: 'a50', label: '50kg_実績' },
  { key: 'm50', label: '50kg_MAX' },
  { key: 'note', label: '備考' },
  { key: 'updatedAt', label: '更新日時' },
  { key: 'updatedBy', label: '更新者' }
];

// 初回作成時の初期データ。既存の「容器置場可能数」表(2021/02時点)から取り込み。
// 実績数は取り込み時点のスナップショットなので、運用開始後は都度アプリから更新する。
var SEED_LOCATIONS = [
  { no: '①', name: '駐車場', position: '南', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 600, note: '' },
  { no: '②', name: '水処理', position: '西', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '③', name: '５０Ｋ工場', position: '北', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 600, note: '' },
  { no: '④', name: '２０Ｋ工場', position: '北', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 600, note: '' },
  { no: '⑤', name: 'ボイラー', position: '西', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 600, note: '' },
  { no: '⑥', name: '焼鈍', position: '東', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 600, note: '' },
  { no: '⑦-1', name: '大型製缶', position: '北・北', a20: 0, m20: 0, a30: 0, m30: 0, a50: 2400, m50: 2400, note: '' },
  { no: '⑦-2', name: '大型製缶', position: '北・南', a20: 0, m20: 0, a30: 0, m30: 0, a50: 1500, m50: 1800, note: '雪置場（▲300）' },
  { no: '⑧', name: '大型製缶', position: '西１', a20: 0, m20: 0, a30: 0, m30: 0, a50: 1000, m50: 1000, note: '雪置場（▲100）' },
  { no: '⑨', name: '大型製缶', position: '西２', a20: 0, m20: 0, a30: 0, m30: 0, a50: 1000, m50: 1000, note: '' },
  { no: '⑩', name: 'コンテナ', position: '西', a20: 1568, m20: 1988, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '⑪', name: '事務所', position: '東', a20: 0, m20: 0, a30: 0, m30: 0, a50: 1600, m50: 1900, note: '雪置場（▲300）' },
  { no: '⑫', name: 'コイル跡地', position: '', a20: 0, m20: 0, a30: 0, m30: 0, a50: 1600, m50: 1600, note: '' },
  { no: '⑬', name: '資材倉庫', position: '北', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '⑭', name: '西下屋', position: '3', a20: 1200, m20: 1200, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '⑮', name: '6号倉庫', position: '前', a20: 500, m20: 0, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '⑯', name: '集会場', position: '西', a20: 800, m20: 0, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '⑰', name: 'トイレ', position: '西', a20: 700, m20: 1400, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '⑱', name: '塗料倉庫', position: '前', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '⑲', name: 'ポンプ室', position: '東', a20: 900, m20: 900, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '⑳', name: '風呂場跡', position: '', a20: 8928, m20: 9936, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '㉑', name: '検収倉庫', position: '西', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '㉒', name: '製品第3倉庫', position: '西', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '㉓', name: '集会場', position: '東', a20: 784, m20: 784, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '㉔', name: '西下屋', position: '2', a20: 500, m20: 500, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '㉕', name: '集会場', position: '北', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '㉖', name: '製品第2倉庫', position: '北', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 0, note: '' },
  { no: '㉗', name: '臨時', position: '池北駐車場', a20: 0, m20: 0, a30: 0, m30: 0, a50: 1200, m50: 1200, note: '' },
  { no: '㉘', name: '臨時', position: '20K北', a20: 0, m20: 0, a30: 0, m30: 0, a50: 600, m50: 600, note: '' },
  { no: '㉘-2', name: 'アセチレン', position: '', a20: 0, m20: 0, a30: 0, m30: 0, a50: 1500, m50: 1500, note: '' },
  { no: '㉙', name: '臨時', position: 'ゴミ置き場', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 600, note: '' },
  { no: '㉚', name: '臨時', position: '工作工場', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 1200, note: '' },
  { no: '㉛', name: '臨時', position: '仕上げ工場', a20: 0, m20: 0, a30: 0, m30: 0, a50: 0, m50: 1200, note: '' },
  { no: '32', name: '地区倉庫', position: '東北', a20: 0, m20: 900, a30: 0, m30: 0, a50: 0, m50: 1700, note: '' },
  { no: '33', name: '地区倉庫', position: '北海道', a20: 0, m20: 1700, a30: 0, m30: 0, a50: 0, m50: 1300, note: '' },
  { no: '34', name: '屋内', position: '', a20: 0, m20: 0, a30: 0, m30: 0, a50: 3000, m50: 3400, note: '' }
];

function getSpreadsheet_() {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('SPREADSHEET_ID');

  if (id) {
    return SpreadsheetApp.openById(id);
  }

  // 初回実行時: スプレッドシートが未設定なら新規作成してScript Propertiesに保存する。
  var ss = SpreadsheetApp.create('LPG容器 屋外在庫管理');
  props.setProperty('SPREADSHEET_ID', ss.getId());
  Logger.log('新規スプレッドシートを作成しました: ' + ss.getUrl());
  return ss;
}

function getSheet_() {
  var ss = getSpreadsheet_();
  var sheet = ss.getSheetByName(SHEET_NAME);

  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    var headers = COLUMNS.map(function (c) { return c.label; });
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);

    var now = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd HH:mm');
    var seedRows = SEED_LOCATIONS.map(function (loc) {
      return COLUMNS.map(function (c) {
        if (c.key === 'updatedAt') return now;
        if (c.key === 'updatedBy') return '(初期データ)';
        return loc[c.key] !== undefined ? loc[c.key] : '';
      });
    });

    if (seedRows.length) {
      sheet.getRange(2, 1, seedRows.length, headers.length).setValues(seedRows);
    }
  }

  return sheet;
}

/**
 * シートの全データをオブジェクトの配列として取得する。
 * 各オブジェクトには行番号(_row)を含める(更新時に使用)。
 */
function readAllRecords_() {
  var sheet = getSheet_();
  var lastRow = sheet.getLastRow();

  if (lastRow < 2) {
    return [];
  }

  var lastCol = COLUMNS.length;
  var values = sheet.getRange(2, 1, lastRow - 1, lastCol).getValues();

  var records = [];

  for (var i = 0; i < values.length; i++) {
    var row = values[i];
    var noCell = row[0];

    if (noCell === '' || noCell === null) {
      continue;
    }

    var record = { _row: i + 2 };

    for (var c = 0; c < COLUMNS.length; c++) {
      record[COLUMNS[c].key] = formatCellValue_(row[c]);
    }

    records.push(record);
  }

  return records;
}

function formatCellValue_(value) {
  if (Object.prototype.toString.call(value) === '[object Date]') {
    return Utilities.formatDate(value, 'Asia/Tokyo', 'yyyy-MM-dd HH:mm');
  }
  return value;
}

function findRowByNo_(no) {
  var sheet = getSheet_();
  var lastRow = sheet.getLastRow();

  if (lastRow < 2) {
    return -1;
  }

  var nos = sheet.getRange(2, 1, lastRow - 1, 1).getValues();

  for (var i = 0; i < nos.length; i++) {
    if (String(nos[i][0]) === String(no)) {
      return i + 2;
    }
  }

  return -1;
}

function columnIndex_(key) {
  for (var i = 0; i < COLUMNS.length; i++) {
    if (COLUMNS[i].key === key) {
      return i + 1;
    }
  }
  throw new Error('不明な列キー: ' + key);
}

function currentUserEmail_() {
  var email = Session.getActiveUser().getEmail();
  return email || '不明';
}
