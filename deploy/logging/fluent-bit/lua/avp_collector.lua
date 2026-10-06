-- Collector の振り分け・型修復・追加の安全化（ADR-0040 §1/§4/§6、log-contract §2/§7）。
--
-- 呼び出し順（fluent-bit.yaml）:
--   docker.*  -> avp_route      : compose project の完全一致で絞り、app / infra を決める
--   (rewrite_tag で avp.app / avp.infra へ)
--   avp.app   -> parser(json, key log) -> avp_app   : 型修復・@timestamp・Collector フィールド
--   avp.infra -> avp_infra                           : 安全化・切り詰め
--
-- 戻り値の規約（Fluent Bit Lua filter）: -1 = 捨てる、1 = 時刻とレコードを変えた、2 = レコードだけ変えた。
-- 型表と定数は契約から生成した contract_types.lua（手で直さない）。

local script_dir = (debug.getinfo(1, "S").source:match("^@(.*/)") or "./")
local C = dofile(script_dir .. "contract_types.lua")

local TARGET_PROJECT = os.getenv("AVP_LOG_TARGET_PROJECT") or ""
local HOST_NAME = os.getenv("AVP_LOG_HOST_NAME") or C.unknown
if HOST_NAME == "" then
  HOST_NAME = C.unknown
end

local ROUTE_KEY = "_avp_route"
local PROJECT_ATTR = "com.docker.compose.project"
local SERVICE_ATTR = "com.docker.compose.service"
local ERR_TIMESTAMP = "@timestamp_replaced"
local ERR_NOT_JSON = "json_parse_failed"
-- 1行（Docker の partial を結合した後）が C.collector_line_max_bytes を超えた（I-15）
local ERR_LINE_TOO_LONG = "line_too_long"
local LINE_TOO_LONG_KEY = "_avp_line_too_long"
-- OpenSearch の _id の上限（bytes）。超えると bulk の request 全体が 400 になり、同じ chunk の
-- 正常な行まで再送の末に破棄される（実測）。超える event_id は退避して自動 ID にする
local ID_MAX_BYTES = 512
-- 出力側は tv_nsec を切り捨ててミリ秒を書く。double の誤差で 1ms 下がらないよう半ミリ秒足す
local HALF_MS = 0.0005
-- event_id を持たない app 系統の記録に付ける ID の接頭辞。Fluent Bit 5.1.2 の opensearch output は
-- id_key の値が無い record に**直前の record の _id** を使い回す（action 行を作り直さない。実測:
-- 409 になり黙って捨てられた）。app 系統では必ず event_id を持たせる
local GENERATED_ID_PREFIX = "collector-"
math.randomseed(os.time())
local ID_SALT = string.format("%x%x", os.time(), math.random(0, 0x7fffffff))
local id_seq = 0

local function generated_id(timestamp)
  id_seq = id_seq + 1
  return string.format("%s%s-%.6f-%d", GENERATED_ID_PREFIX, ID_SALT, timestamp or 0, id_seq)
end

-- ---------------------------------------------------------------- 文字列の安全化（追加防御）

-- キー名を含むもの（log-contract §7.2 の部分一致キー）。`key=value` / `"key": "value"` 形式を伏せる
local SECRET_KEY_WORDS = {
  "authorization", "cookie", "token", "secret", "password", "passwd", "api_key", "apikey",
  "credential", "private_key", "dsn", "database_url", "connection_string", "signature",
  "fal_key", "session_uri", "upload_url", "access_key",
}

local function redact_long(pattern_min)
  return function(s)
    if #s >= pattern_min then
      return C.redacted
    end
    return s
  end
end

-- open から close まで（どちらもパターン）を伏せる。close が無ければ行末まで伏せる。
-- `open.-close` は close の無い open が多いと O(n²) になるので、find を前へ進めるだけにする（I-15）
local function redact_between(open_pat, close_pat, replacement)
  return function(s)
    if not s:find(open_pat) then
      return s
    end
    local parts, pos = {}, 1
    while true do
      local i, j = s:find(open_pat, pos)
      if i == nil then
        break
      end
      parts[#parts + 1] = s:sub(pos, i - 1)
      parts[#parts + 1] = replacement
      local _, e = s:find(close_pat, j + 1)
      if e == nil then
        return table.concat(parts)
      end
      pos = e + 1
    end
    parts[#parts + 1] = s:sub(pos)
    return table.concat(parts)
  end
end

local SCHEME_BYTE = {}
for c in ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-"):gmatch(".") do
  SCHEME_BYTE[c:byte()] = true
end

-- userinfo 付きの URL / DSN（scheme に + を含む形も）。`scheme://user:pass@` の `user:pass` を伏せる。
-- 先頭を英字の連なりで探す gsub は長い英数字の連なりで各開始位置から末尾まで読み戻すので O(n²) になり、300k の
-- 1行で Collector のイベントループ（全ファイルの tail）が止まった（実測、I-15）。`://` を起点に
-- scheme を後ろへ、userinfo を前へ読む（どちらも次の `://` を越えないので全体で線形）
local function redact_userinfo(s)
  if not s:find("://", 1, true) then
    return s
  end
  local parts, pos, init = {}, 1, 1
  while true do
    local i = s:find("://", init, true)
    if i == nil then
      break
    end
    local after = i + 3
    local j, letter = i - 1, false
    while j >= 1 and SCHEME_BYTE[s:byte(j)] do
      local b = s:byte(j)
      if (b >= 65 and b <= 90) or (b >= 97 and b <= 122) then
        letter = true
      end
      j = j - 1
    end
    local e = nil
    if letter then
      local _, ue = s:find("^[^/%s:@\"']*:[^/%s@\"']*@", after)
      e = ue
    end
    if e ~= nil then
      parts[#parts + 1] = s:sub(pos, after - 1)
      parts[#parts + 1] = C.redacted .. "@"
      pos = e + 1
      init = e + 1
    else
      init = after
    end
  end
  parts[#parts + 1] = s:sub(pos)
  return table.concat(parts)
end

-- JWT（3つの部分を . で繋いだ token）。`eyJ[...]+%.[...]+%.[...]+` は `.` の無い長い連なりで O(n²) に
-- なるので、`.` を含む連なりを1回で取り、形を確かめてから伏せる（I-15）
local function redact_jwt(token)
  if token:find("^eyJ[%w%-_]+%.[%w%-_]+%.[%w%-_]+") then
    return C.redacted
  end
  return token
end

-- 値のパターン（§7.3）。Lua のパターンは正規表現ではない（大文字小文字の区別、量指定子の制約）。
-- 規則は長い行でも線形であること（失敗した照合が開始位置ごとに末尾まで読み戻す形を使わない。I-15）。
-- { pattern, replacement } は gsub、{ fn } は文字列全体を受け取る関数
local VALUE_RULES = {
  -- PEM の秘密鍵ブロック（END が無ければ行末まで）
  { redact_between("%-%-%-%-%-BEGIN[%u ]*PRIVATE KEY%-%-%-%-%-", "PRIVATE KEY%-%-%-%-%-", C.redacted) },
  -- Authorization の値
  { "([Bb]earer)%s+[%w%-%._~%+/=]+", "%1 " .. C.redacted },
  { "([Bb]asic)%s+[%w%+/=]+", "%1 " .. C.redacted },
  -- Authorization ヘッダの値は形式を問わず行末・引用符まで伏せる（`Key <fal key>` 等）
  { "([Aa]uthorization[\"']?%s*[:=]%s*[\"']?)[^\"'\r\n]+", "%1" .. C.redacted },
  -- JWT
  { "eyJ[%w%-_%.]*", redact_jwt },
  -- userinfo 付きの URL / DSN（空の user も）
  { redact_userinfo },
  -- Google OAuth
  { "ya29%.[%w%-_%.]+", C.redacted },
  { "1//[%w%-_]+", redact_long(12) },
  -- sk-... 形式
  { "sk%-[%w%-_]+", redact_long(20) },
  -- fal key（uuid:hex）
  { "%x%x%x%x%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x:%x+", C.redacted },
  -- SQLAlchemy の [parameters: ...]（] が無ければ行末まで）
  { redact_between("%[parameters: ", "%]", "[parameters: " .. C.redacted .. "]") },
  -- URL の query・fragment（署名付き URL の署名を残さない）。2つ目の捕捉は空か ?/# で始まり、
  -- 必ず成功するので読み戻さない
  { "(https?://[^%s%?#\"'<>]+)[^%s\"'<>]*", "%1" },
  -- 長い base64 様の値（256字以上）
  { "[%w%+/=_%-]+", redact_long(256) },
}

local function sanitize(s)
  if type(s) ~= "string" or s == "" then
    return s, false
  end
  local out = s
  for _, rule in ipairs(VALUE_RULES) do
    if rule[2] == nil then
      out = rule[1](out)
    else
      out = out:gsub(rule[1], rule[2])
    end
  end
  local lower = out:lower()
  for _, word in ipairs(SECRET_KEY_WORDS) do
    if lower:find(word, 1, true) then
      -- word に続く `: value` / `=value`（引用符つきも）を伏せる。大文字小文字は両方を試す
      for _, w in ipairs({ word, word:upper(), (word:gsub("^%l", string.upper)) }) do
        local esc = w:gsub("%p", "%%%0")
        out = out:gsub("(" .. esc .. "[%w_%-]*[\"']?%s*[:=]%s*[\"']?)[^%s\"'&,;}]+", "%1" .. C.redacted)
      end
      lower = out:lower()
    end
  end
  return out, out ~= s
end

-- UTF-8 の文字境界で max_bytes 以下に切る
local function truncate_utf8(s, max_bytes)
  if #s <= max_bytes then
    return s, false
  end
  local cut = max_bytes
  -- 継続バイト（10xxxxxx）の途中なら先頭バイトまで戻る
  while cut > 0 do
    local b = s:byte(cut + 1)
    if b == nil or b < 0x80 or b >= 0xC0 then
      break
    end
    cut = cut - 1
  end
  return s:sub(1, cut), true
end

-- ---------------------------------------------------------------- 時刻

local function days_from_civil(y, m, d)
  y = (m <= 2) and (y - 1) or y
  local era = math.floor(y / 400)
  local yoe = y - era * 400
  local mp = (m + 9) % 12
  local doy = math.floor((153 * mp + 2) / 5) + d - 1
  local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
  return era * 146097 + doe - 719468
end

-- `YYYY-MM-DDTHH:MM:SS[.fff…](Z|±HH:MM)` を epoch 秒（小数）へ。解釈できなければ nil
local function parse_iso8601(s)
  if type(s) ~= "string" then
    return nil
  end
  local y, mo, d, h, mi, sec, rest =
    s:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)[T ](%d%d):(%d%d):(%d%d)(.*)$")
  if not y then
    return nil
  end
  y, mo, d, h, mi, sec = tonumber(y), tonumber(mo), tonumber(d), tonumber(h), tonumber(mi), tonumber(sec)
  if mo < 1 or mo > 12 or d < 1 or d > 31 or h > 23 or mi > 59 or sec > 60 then
    return nil
  end
  local frac = 0
  local fs, tz = rest:match("^%.(%d+)(.*)$")
  if fs then
    frac = tonumber("0." .. fs)
    rest = tz
  end
  local offset = 0
  if rest == "Z" or rest == "z" then
    offset = 0
  else
    local sign, oh, om = rest:match("^([%+%-])(%d%d):?(%d%d)$")
    if not sign then
      return nil
    end
    offset = (tonumber(oh) * 3600 + tonumber(om) * 60) * ((sign == "-") and -1 or 1)
  end
  return days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + sec + frac - offset
end

-- ---------------------------------------------------------------- 型修復

local function is_array(t)
  if type(t) ~= "table" then
    return false
  end
  local n = 0
  for k, _ in pairs(t) do
    if type(k) ~= "number" then
      return false
    end
    n = n + 1
  end
  return n == #t
end

local function all_scalars(t)
  for _, v in ipairs(t) do
    local tv = type(v)
    if tv ~= "string" and tv ~= "number" and tv ~= "boolean" then
      return false
    end
  end
  return true
end

-- (ok, value)。ok=false なら退避する
local function coerce(ftype, v)
  local tv = type(v)
  if ftype == "keyword" then
    if tv == "string" then
      return true, v
    elseif tv == "number" or tv == "boolean" then
      return true, tostring(v)
    elseif is_array(v) and all_scalars(v) then
      local out = {}
      for i, e in ipairs(v) do
        out[i] = (type(e) == "string") and e or tostring(e)
      end
      return true, out
    end
    return false, v
  elseif ftype == "text" then
    if tv == "string" then
      return true, v
    elseif tv == "number" or tv == "boolean" then
      return true, tostring(v)
    end
    return false, v
  elseif ftype == "integer" or ftype == "long" or ftype == "double" then
    if tv == "number" then
      return true, v
    elseif tv == "string" and tonumber(v) ~= nil then
      return true, tonumber(v)
    end
    return false, v
  elseif ftype == "boolean" then
    if tv == "boolean" then
      return true, v
    elseif v == "true" then
      return true, true
    elseif v == "false" then
      return true, false
    end
    return false, v
  elseif ftype == "date" then
    if tv == "string" or tv == "number" then
      return true, v
    end
    return false, v
  elseif ftype == "opaque_object" then
    return true, v
  end
  return false, v
end

local TEXT_TO_SANITIZE = { "message", "error_message", "response_excerpt", "exception_stack" }

local function set_collector_fields(record, attrs, source)
  record.log_source = source
  record.container_name = attrs.tag and (attrs.tag:gsub("^/", "")) or nil
  record.compose_service = attrs[SERVICE_ATTR]
  record.compose_project = attrs[PROJECT_ATTR]
  record.host_name = HOST_NAME
end

-- ---------------------------------------------------------------- filter の入口

function avp_route(tag, timestamp, record)
  local attrs = record.attrs
  if type(attrs) ~= "table" or TARGET_PROJECT == "" or attrs[PROJECT_ATTR] ~= TARGET_PROJECT then
    return -1, timestamp, record
  end
  local route = "infra"
  if type(record.log) == "string" and #record.log > C.collector_line_max_bytes then
    -- partial を結合した後の行には tail の buffer_max_size が効かない（実測、I-15）。JSON として
    -- 解釈せず（切った JSON は壊れている）、安全化の前にここで切って infra へ送る
    record.log = truncate_utf8(record.log, C.collector_line_max_bytes)
    record[LINE_TOO_LONG_KEY] = true
  elseif attrs[C.app_label] == C.app_label_value and record.stream == "stdout"
    and type(record.log) == "string" and record.log:match("^%s*{") then
    route = "app"
  end
  record[ROUTE_KEY] = route
  return 2, timestamp, record
end

local function unstructured(timestamp, record, attrs, extra_error)
  local line = record.log
  if type(line) ~= "string" then
    line = tostring(line)
  end
  line = line:gsub("[\r\n]+$", "")
  local clean, redacted = sanitize(line)
  local cut, truncated = truncate_utf8(clean, C.unstructured_line_max_bytes)
  local out = {
    message = cut,
    stream = record.stream,
    redaction_applied = redacted,
    truncated = truncated,
  }
  set_collector_fields(out, attrs, C.log_source.unstructured)
  if extra_error then
    out.collector_errors = { extra_error }
  end
  return 1, timestamp, out
end

function avp_infra(tag, timestamp, record)
  local attrs = record.attrs or {}
  if record[LINE_TOO_LONG_KEY] then
    local code, ts, out = unstructured(timestamp, record, attrs, ERR_LINE_TOO_LONG)
    out.truncated = true
    return code, ts, out
  end
  return unstructured(timestamp, record, attrs, nil)
end

function avp_app(tag, timestamp, record)
  local attrs = record.attrs or {}
  -- parser が JSON として解釈できなかった行（log が残っている）は unstructured として扱う
  if record.log ~= nil then
    local code, ts, out = unstructured(timestamp, record, attrs, ERR_NOT_JSON)
    out.event_id = generated_id(timestamp)
    return code, ts, out
  end

  local out = {}
  local moved = {}
  local errors = {}
  local stream = record.stream

  for k, v in pairs(record) do
    if k == "attrs" or k == "time" or k == ROUTE_KEY or k == "stream" then
      -- Docker / Collector の包装。捨てる（stream は下で Collector フィールドとして付け直す）
    elseif C.collector_fields[k] then
      -- Collector が書くフィールドをアプリが書いていたら退避（上書きで失わない）
      moved[k] = v
      errors[#errors + 1] = k
    else
      local ftype = C.fields[k]
      if ftype == nil then
        moved[k] = v
        errors[#errors + 1] = k
      else
        local ok, value = coerce(ftype, v)
        if ok then
          out[k] = value
        else
          moved[k] = v
          errors[#errors + 1] = k
        end
      end
    end
  end

  -- event_id は _id になる（id_key）。_id にできない値は退避する（Fluent Bit は制御文字・引用符だけを検査する）
  local eid = out.event_id
  if eid ~= nil and (type(eid) ~= "string" or eid == "" or #eid > ID_MAX_BYTES
    or eid:find("[%c\"\\]")) then
    moved.event_id = eid
    out.event_id = nil
    errors[#errors + 1] = "event_id"
  end
  if out.event_id == nil then
    out.event_id = generated_id(timestamp)
  end

  -- @timestamp: 解釈できなければ Docker の時刻に置き換える。出力側が record の時刻から書く
  local ts = parse_iso8601(out[C.time_field])
  if ts ~= nil then
    ts = ts + HALF_MS
  else
    ts = timestamp
    errors[#errors + 1] = ERR_TIMESTAMP
    if out[C.time_field] ~= nil then
      moved[C.time_field] = out[C.time_field]
    end
  end
  out[C.time_field] = nil

  -- 追加の安全化（整形器の取りこぼしへの防御）
  local redacted = false
  for _, name in ipairs(TEXT_TO_SANITIZE) do
    if type(out[name]) == "string" then
      local clean, changed = sanitize(out[name])
      if changed then
        out[name] = clean
        redacted = true
      end
    end
  end
  if redacted then
    out.redaction_applied = true
  end

  if next(moved) ~= nil then
    local attributes = out.attributes
    if type(attributes) ~= "table" or is_array(attributes) then
      if attributes ~= nil then
        moved.attributes = attributes
      end
      attributes = {}
    end
    attributes.collector_moved = moved
    out.attributes = attributes
  end
  if #errors > 0 then
    table.sort(errors)
    out.collector_errors = errors
  end

  out.stream = stream
  set_collector_fields(out, attrs, C.log_source.app_json)
  return 1, ts, out
end
