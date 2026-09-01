/**
 * LPG容器 屋外在庫管理アプリ
 * エントリーポイントと、クライアント(HTML)から呼ばれるAPI関数。
 */

function doGet() {
  return HtmlService.createTemplateFromFile('Index')
    .evaluate()
    .setTitle('LPG容器 在庫管理')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

function include(filename) {
  return HtmlService.createHtmlOutputFromFile(filename).getContent();
}

var BUILDING_OVERRIDES_PROP = 'BUILDING_OVERRIDES';

/**
 * 敷地図の建屋(SITE_BUILDINGS/SITE_BUILDING_SHAPES)に対する、
 * ユーザーが調整した位置・向き・大きさの上書き情報を返す。
 * { [buildingId]: { dx, dy, rotation, scale } }
 */
function getBuildingOverrides() {
  var json = PropertiesService.getScriptProperties().getProperty(BUILDING_OVERRIDES_PROP);
  return json ? JSON.parse(json) : {};
}

/**
 * 建屋1つ分の上書き情報を保存する。override に null を渡すと、その建屋の
 * 上書きを削除して初期状態に戻す。
 */
function saveBuildingOverride(buildingId, override) {
  if (!buildingId) {
    throw new Error('建物のIDが指定されていません');
  }

  var lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    var props = PropertiesService.getScriptProperties();
    var all = getBuildingOverrides();

    if (override === null) {
      delete all[buildingId];
    } else {
      all[buildingId] = override;
    }

    props.setProperty(BUILDING_OVERRIDES_PROP, JSON.stringify(all));
    return { ok: true };
  } finally {
    lock.releaseLock();
  }
}

/**
 * フィルタ条件に合致する置場一覧を返す。
 * filters: { keyword, onlyNearFull }
 * onlyNearFull=true の場合、いずれかのサイズの充填率が80%以上の置場だけを返す。
 */
function getInventory(filters) {
  filters = filters || {};
  var keyword = (filters.keyword || '').trim().toLowerCase();
  var onlyNearFull = !!filters.onlyNearFull;

  var records = readAllRecords_();

  var result = records.filter(function (r) {
    if (keyword) {
      var haystack = [r.no, r.name, r.position, r.note].join(' ').toLowerCase();
      if (haystack.indexOf(keyword) === -1) {
        return false;
      }
    }
    if (onlyNearFull && maxUtilization_(r) < 0.8) {
      return false;
    }
    return true;
  });

  result.sort(function (a, b) {
    return String(a.no).localeCompare(String(b.no), 'ja', { numeric: true });
  });

  return result;
}

function maxUtilization_(r) {
  var best = 0;
  SIZES.forEach(function (s) {
    var actual = Number(r['a' + s.key]) || 0;
    var max = Number(r['m' + s.key]) || 0;
    if (max > 0) {
      best = Math.max(best, actual / max);
    }
  });
  return best;
}

/**
 * 新規置場を登録する。
 * data: { no, name, position, note, a20, m20, a30, m30, a50, m50 }
 */
function addLocation(data) {
  if (!data || !data.no) {
    throw new Error('番号は必須です');
  }

  var lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    if (findRowByNo_(data.no) !== -1) {
      throw new Error('番号「' + data.no + '」は既に登録されています');
    }

    var sheet = getSheet_();
    var now = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd HH:mm');

    var row = COLUMNS.map(function (c) {
      switch (c.key) {
        case 'no': return data.no;
        case 'name': return data.name || '';
        case 'position': return data.position || '';
        case 'note': return data.note || '';
        case 'a20': return Number(data.a20) || 0;
        case 'm20': return Number(data.m20) || 0;
        case 'a30': return Number(data.a30) || 0;
        case 'm30': return Number(data.m30) || 0;
        case 'a50': return Number(data.a50) || 0;
        case 'm50': return Number(data.m50) || 0;
        case 'updatedAt': return now;
        case 'updatedBy': return currentUserEmail_();
        default: return '';
      }
    });

    sheet.appendRow(row);
    return { ok: true };
  } finally {
    lock.releaseLock();
  }
}

/**
 * 既存置場の番号を、現在の並び順のまま 1, 2, 3... の単純な通し番号に振り直す。
 * 番号以外のデータ(実績・MAX・地図上の位置など)は変更しない。
 */
function renumberLocationsSequentially() {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    var sheet = getSheet_();
    var lastRow = sheet.getLastRow();

    if (lastRow < 2) {
      return { ok: true, count: 0 };
    }

    var noCol = columnIndex_('no');
    var range = sheet.getRange(2, noCol, lastRow - 1, 1);
    var values = range.getValues();
    var newValues = [];
    var n = 0;

    for (var i = 0; i < values.length; i++) {
      if (values[i][0] === '' || values[i][0] === null) {
        newValues.push(['']);
        continue;
      }
      n++;
      newValues.push([n]);
    }

    range.setValues(newValues);
    return { ok: true, count: n };
  } finally {
    lock.releaseLock();
  }
}

/**
 * 既存置場の情報を更新する(実績数・MAX・位置・備考など)。
 * updates に渡されたキーだけ更新する。
 */
function updateLocation(no, updates) {
  if (!no) {
    throw new Error('番号が指定されていません');
  }

  var lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    var rowNum = findRowByNo_(no);

    if (rowNum === -1) {
      throw new Error('番号「' + no + '」が見つかりません');
    }

    var sheet = getSheet_();
    var numericKeys = ['a20', 'm20', 'a30', 'm30', 'a50', 'm50', 'mapX', 'mapY'];
    var textKeys = ['name', 'position', 'note'];

    numericKeys.forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(updates, key)) {
        var v = Number(updates[key]);
        sheet.getRange(rowNum, columnIndex_(key)).setValue(isNaN(v) ? 0 : v);
      }
    });

    textKeys.forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(updates, key)) {
        sheet.getRange(rowNum, columnIndex_(key)).setValue(updates[key]);
      }
    });

    var now = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd HH:mm');
    sheet.getRange(rowNum, columnIndex_('updatedAt')).setValue(now);
    sheet.getRange(rowNum, columnIndex_('updatedBy')).setValue(currentUserEmail_());

    return { ok: true };
  } finally {
    lock.releaseLock();
  }
}
