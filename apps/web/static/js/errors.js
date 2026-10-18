// エラーコード → 日本語の理由と「次にすること」。サーバー側の唯一の宣言元は
// contracts/application.py の OperationErrorCode と apps/api/routers/application.py の ERROR_STATUS。
// tests/web/errors.test.mjs が OperationErrorCode の全値がここにあることを検査する。

export class ApiError extends Error {
  constructor({ status, code = null, serverMessage = null, detail = null, correlationId = null }) {
    super(serverMessage || code || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status; // 0 = ネットワーク失敗
    this.code = code;
    this.serverMessage = serverMessage;
    this.detail = detail;
    this.correlationId = correlationId;
  }
}

//  reload: 画面の状態が古い。再読み込みして見直す
//  retry : 同じ操作の再試行で成功しうる
export const ERROR_INFO = {
  invalid_payload: {
    reason: '入力内容がサーバーの規則に合いません。',
    action: '入力を見直してください。',
  },
  unsupported_change: {
    reason: 'この変更はまだ対応していません（P2 で編集できるのは visual_description だけです）。',
    action: '変更内容を visual_description のみにしてください。',
  },
  missing_scope: {
    reason: 'あなたのトークンには、この操作に必要な権限（scope）がありません。',
    action: '必要な scope を持つトークンで入り直してください。',
  },
  episode_not_found: {
    reason: 'この Episode が見つかりません。',
    action: '一覧に戻って ID を確認してください。',
  },
  command_id_reused: {
    reason: '同じ command_id が別の内容で使われています。',
    action: '画面を開き直して、新しい操作として送り直してください。',
  },
  stale_control_revision: {
    reason: '画面を開いた後に、他の操作で Episode の制御状態が変わりました。',
    action: '最新の状態を読み込み直しました。内容を確認してからやり直してください。',
    reload: true,
  },
  stale_target_revision: {
    reason: '変更の対象（絵コンテなど）の版が、提案時から変わっています。',
    action: '最新の状態を読み込み直しました。変更案を作り直してください。',
    reload: true,
  },
  forbidden: {
    reason: 'この操作はあなたの権限では許可されていません（人だけが実行できる操作の場合を含みます）。',
    action: '権限のある利用者に依頼してください。',
  },
  budget_exceeded: {
    reason: '承認した上限額を、見積りが超えています。',
    action: '見積りを確認し、上限額を見直してください。',
  },
  budget_unverifiable: {
    reason: '費用を確認できないため、適用できません。',
    action: '時間をおいて見積りを取り直してください。',
  },
  feature_disabled: {
    reason: 'この API は無効化されています（JPMAS2_APPLICATION_API）。',
    action: '運用者に有効化を確認してください。',
  },
  unknown_template: {
    reason: '指定された workflow テンプレートが存在しません。',
    action: 'テンプレート名を確認してください。',
  },
  unknown_agent_spec: {
    reason: '指定された Agent 仕様が存在しません。',
    action: 'Agent 仕様の名前・版を確認してください。',
  },
  template_not_runnable: {
    reason: 'このテンプレートは今は実行できません。',
    action: 'テンプレートの状態を運用者に確認してください。',
  },
  invalid_state: {
    reason: '今の Episode の状態では、この操作はできません。',
    action: '最新の状態を読み込み直しました。状態を確認してください。',
    reload: true,
  },
  change_set_not_found: {
    reason: '変更案が見つかりません。',
    action: '最新の状態を読み込み直しました。一覧から選び直してください。',
    reload: true,
  },
  tool_call_not_found: {
    reason: 'この tool 呼び出しが見つかりません。',
    action: '一覧から選び直してください。',
    reload: true,
  },
  integrity_error: {
    reason: '保存された tool の結果が記録と一致しません（改ざんまたは欠落の可能性）。',
    action: 'この結果は信頼せず、運用担当に知らせてください。',
  },
  cost_not_estimated: {
    reason: '費用が見積もられていない変更は適用できません（未計測を 0 円として扱いません）。',
    action: '見積りが付くまで適用しないでください。',
  },
  // command の reject_reason（contracts/intervention.py RejectReason）のうち上と重ならないもの
  unsupported_command: { reason: 'この種類の command は対応していません。', action: '運用者に確認してください。' },
  user_constraint_violation: { reason: '利用者が設定した制約に反します。', action: '制約と変更内容を見直してください。' },
  not_found: { reason: '対象が見つかりません。', action: '最新の状態を読み込み直してください。' },
  internal_error: { reason: 'サーバー内部のエラーです。', action: '時間をおいて再試行し、続くなら相関 ID を添えて報告してください。' },
};

// HTTP / 通信レベル（OperationErrorCode に無いもの）
export const HTTP_ERROR_INFO = {
  unauthorized: {
    reason: '認証に失敗しました（トークンが無い・誤り・期限切れ）。',
    action: 'トークンを入力し直してください。',
  },
  validation: {
    reason: 'リクエストの形式がサーバーの検証を通りませんでした。',
    action: '入力を見直してください。',
  },
  rate_limited: {
    reason: 'リクエストが多すぎます。',
    action: 'しばらく待ってから再試行してください。',
    retry: true,
  },
  // 408 / 425: リクエストが処理されたか分からない・早すぎた。同じ command_id で再試行してよい（冪等）
  request_timeout: {
    reason: 'サーバーがリクエストを待ちきれませんでした（408）。処理されたかどうか分かりません。',
    action: '同じ操作として再試行してください（二重には実行されません）。',
    retry: true,
  },
  too_early: {
    reason: 'リクエストが早すぎるため受け付けられませんでした（425）。',
    action: '少し待って、同じ操作として再試行してください。',
    retry: true,
  },
  server_error: {
    reason: 'サーバーでエラーが起きました。',
    action: '時間をおいて再試行してください。続く場合は相関 ID を添えて報告してください。',
    retry: true,
  },
  network_error: {
    reason: 'サーバーに接続できませんでした。入力内容は保持されています。',
    action: 'ネットワークを確認して、再試行してください。',
    retry: true,
  },
  not_found: { reason: '対象が見つかりません。', action: 'URL と ID を確認してください。' },
  conflict: { reason: '状態が競合しました。', action: '最新の状態を読み込み直してください。', reload: true },
  unknown: { reason: '想定していないエラーです。', action: '相関 ID を添えて報告してください。' },
};

function httpKey(err) {
  if (err.status === 0) return 'network_error';
  if (err.status === 401) return 'unauthorized';
  if (err.status === 404) return 'not_found';
  if (err.status === 409) return 'conflict';
  if (err.status === 408) return 'request_timeout';
  if (err.status === 425) return 'too_early';
  if (err.status === 422) return 'validation';
  if (err.status === 429) return 'rate_limited';
  if (err.status >= 500) return 'server_error';
  return 'unknown';
}

/** ApiError → 表示用の {title, reason, action, code, status, correlationId, reload, retry}。 */
export function describeError(err) {
  const e = err instanceof ApiError ? err : new ApiError({ status: -1, serverMessage: String(err?.message ?? err) });
  let info;
  let key;
  if (e.code && Object.hasOwn(ERROR_INFO, e.code)) {
    info = ERROR_INFO[e.code];
    key = e.code;
  } else {
    key = httpKey(e);
    info = HTTP_ERROR_INFO[key];
  }
  // 未知の code でも、HTTP の分類で理由を出す（code はそのまま見せる）
  return {
    title: '操作できませんでした',
    reason: info.reason,
    action: info.action,
    code: e.code ?? key,
    status: e.status,
    serverMessage: e.serverMessage,
    correlationId: e.correlationId,
    reload: Boolean(info.reload),
    retry: Boolean(info.retry),
  };
}

/** command の reject_reason / OperationErrorCode の文字列だけから説明を引く（未知は null）。 */
export function describeReason(code) {
  return Object.hasOwn(ERROR_INFO, code) ? ERROR_INFO[code] : null;
}

/** 422 の detail 配列（FastAPI）→ 「場所: 理由」の文字列配列。 */
export function validationLines(detail) {
  if (!Array.isArray(detail)) return [];
  return detail.slice(0, 10).map((d) => {
    const loc = Array.isArray(d?.loc) ? d.loc.filter((x) => x !== 'body').join('.') : '';
    return `${loc || '(場所不明)'}: ${d?.msg ?? '不正な値'}`;
  });
}
