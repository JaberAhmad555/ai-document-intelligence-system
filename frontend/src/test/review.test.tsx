/** Synthetic UI and HTTP-contract tests; no live server or Gemini requests. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ApiError, createApi } from '../api';
import { DocumentDetail } from '../DocumentDetail';
import { CorrectionControls, RetryControl } from '../ReviewControls';
import { EXTRACTION, ID, OTHER_ID, documentFixture, fakeApi, invoiceFixture, validationFixture } from './fixtures';

const REQUEST = '44444444-4444-4444-8444-444444444444';
function receipt() { return { id: OTHER_ID, request_id: REQUEST, document_id: ID, extraction_id: EXTRACTION, revision: 1, created_at: '2026-10-30T09:00:00Z' }; }
function respond(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
function command() { return { request_id: REQUEST, extraction_id: EXTRACTION, expected_revision: 0, reviewer_label: 'Synthetic reviewer', reason: 'Verified source total', changes: { total: '110.00' } }; }
function enableIds() { vi.stubGlobal('crypto', { randomUUID: () => REQUEST }); }
async function openEditor(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Correct invoice values' }));
  await user.type(screen.getByLabelText('Reviewer label (self-reported)'), 'Synthetic reviewer');
  await user.type(screen.getByLabelText('Reason for correction'), 'Verified source total');
}
async function changeTotal(user: ReturnType<typeof userEvent.setup>, value = '110.00') {
  await user.clear(screen.getByLabelText('total'));
  await user.type(screen.getByLabelText('total'), value);
}


describe('review HTTP contracts', () => {
  it('posts exact decimal strings and binds the returned receipt', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(respond(receipt()));
    vi.stubGlobal('fetch', fetcher);
    expect(await createApi('/').correctInvoice(ID, command())).toEqual(receipt());
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe('/api/documents/' + ID + '/corrections');
    expect(init?.method).toBe('POST');
    expect(new Headers(init?.headers).get('Content-Type')).toBe('application/json');
    expect(JSON.parse(String(init?.body))).toEqual(command());
    fetcher.mockResolvedValueOnce(respond({ ...receipt(), request_id: OTHER_ID }));
    await expect(createApi('/').correctInvoice(ID, command())).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('binds validation to the correction revision as well as the extraction', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(respond({ ...validationFixture(), correction_revision: 2 }));
    vi.stubGlobal('fetch', fetcher);
    await expect(createApi('/').validation(ID, EXTRACTION, undefined, 1)).rejects.toMatchObject({ code: 'correction_changed' });
    expect(String(fetcher.mock.calls[0][0])).toContain('correction_revision=1');
  });

  it('uses server pagination for history and validates detailed revision identity', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(respond({ document_id: ID, extraction_id: EXTRACTION, items: [], total: 5, offset: 5, limit: 5, has_more: false }));
    vi.stubGlobal('fetch', fetcher);
    await createApi('/').corrections(ID, EXTRACTION, 5);
    expect(String(fetcher.mock.calls[0][0])).toContain('offset=5');
    expect(String(fetcher.mock.calls[0][0])).toContain('extraction_id=' + EXTRACTION);
    fetcher.mockResolvedValueOnce(respond({ ...receipt(), reviewer_label: 'Reviewer', reason: 'Synthetic', changed_fields: ['total'], original_digest: 'a'.repeat(64), tolerance: '0.02', corrected_fields: ['total'], before_values: { total: '120.00' }, after_values: { total: '110.00' }, issues_at_save: [] }));
    await expect(createApi('/').correction(ID, EXTRACTION, 2)).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('posts a failure-bound idempotent retry without automatically resending', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(respond({ error: { code: 'job_not_retryable', message: 'Already queued.' } }, 409));
    vi.stubGlobal('fetch', fetcher);
    const retry = { request_id: REQUEST, failed_job_id: OTHER_ID };
    await expect(createApi('/').reprocess(ID, retry)).rejects.toMatchObject({ status: 409, code: 'job_not_retryable' });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual(retry);
    fetcher.mockResolvedValueOnce(respond({ request_id: REQUEST, document_id: ID, failed_job_id: OTHER_ID, new_job_id: EXTRACTION, created_at: '2026-10-30T09:00:00Z' }, 202));
    expect((await createApi('/').reprocess(ID, retry)).new_job_id).toBe(EXTRACTION);
  });
});


it('saves an audited correction using exact values, then refreshes published results', async () => {
  enableIds();
  const api = fakeApi(); const reload = vi.fn(); const user = userEvent.setup();
  render(<CorrectionControls api={api} response={invoiceFixture()} reload={reload} />);
  await openEditor(user); await changeTotal(user);
  await user.click(screen.getByRole('button', { name: 'Save correction' }));
  await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  expect(api.correctInvoice).toHaveBeenCalledWith(ID, command());
});


it('requires actual changes and rejects malformed decimal input before sending', async () => {
  enableIds();
  const api = fakeApi(); const user = userEvent.setup();
  render(<CorrectionControls api={api} response={invoiceFixture()} reload={vi.fn()} />);
  await openEditor(user);
  await user.click(screen.getByRole('button', { name: 'Save correction' }));
  expect(await screen.findByText('Change at least one invoice value before saving.')).toBeInTheDocument();
  await changeTotal(user, '1,100.00');
  await user.click(screen.getByRole('button', { name: 'Save correction' }));
  expect(await screen.findByText(/must be an exact decimal without commas/)).toBeInTheDocument();
  expect(api.correctInvoice).not.toHaveBeenCalled();
});


it('sends explicit nulls and line-item replacement without calculating amounts', async () => {
  enableIds();
  const api = fakeApi(); const user = userEvent.setup();
  render(<CorrectionControls api={api} response={invoiceFixture()} reload={vi.fn()} />);
  await openEditor(user);
  await user.clear(screen.getByLabelText('currency'));
  await user.clear(screen.getByLabelText('Line 1 quantity'));
  await user.type(screen.getByLabelText('Line 1 quantity'), '3');
  await user.click(screen.getByRole('button', { name: 'Save correction' }));
  await waitFor(() => expect(api.correctInvoice).toHaveBeenCalledTimes(1));
  const changes = api.correctInvoice.mock.calls[0][1].changes;
  expect(changes.currency).toBeNull();
  expect(changes.line_items?.[0]).toMatchObject({ quantity: '3', unit_price: '50.00', line_total: '100.00' });
  expect(changes.line_items_complete).toBe(true);
});


it('paginates editable rows without submitting the correction form', async () => {
  const api = fakeApi(); const user = userEvent.setup(); const response = invoiceFixture();
  response.result.invoice.line_items = Array.from({ length: 11 }, () => ({ description: 'Synthetic row', quantity: '1', unit_price: '1', line_total: '1' }));
  render(<CorrectionControls api={api} response={response} reload={vi.fn()} />);
  await openEditor(user);
  await user.click(screen.getByRole('button', { name: 'Next lines' }));
  expect(screen.getByLabelText('Line 11 quantity')).toBeInTheDocument();
  expect(api.correctInvoice).not.toHaveBeenCalled();
});


it('keeps the draft on stale revision conflict and requires an explicit reload', async () => {
  enableIds();
  const api = fakeApi(); const reload = vi.fn(); const user = userEvent.setup();
  api.correctInvoice.mockRejectedValue(new ApiError('Another reviewer saved changes.', 409, 'correction_changed'));
  render(<CorrectionControls api={api} response={invoiceFixture()} reload={reload} />);
  await openEditor(user); await changeTotal(user);
  await user.click(screen.getByRole('button', { name: 'Save correction' }));
  expect(await screen.findByText('Another reviewer saved changes.')).toBeInTheDocument();
  expect(screen.getByLabelText('total')).toHaveValue('110.00');
  expect(screen.getByRole('button', { name: 'Save correction' })).toBeDisabled();
  expect(reload).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: 'Reload latest invoice' }));
  expect(reload).toHaveBeenCalledTimes(1);
});


it('reuses the correction request ID after an uncertain network failure', async () => {
  enableIds();
  const api = fakeApi(); const reload = vi.fn(); const user = userEvent.setup();
  api.correctInvoice.mockRejectedValueOnce(new ApiError('Connection lost.', 0));
  render(<CorrectionControls api={api} response={invoiceFixture()} reload={reload} />);
  await openEditor(user); await changeTotal(user);
  await user.click(screen.getByRole('button', { name: 'Save correction' }));
  expect(await screen.findByText(/server may have saved this request/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Save correction' }));
  await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  expect(api.correctInvoice.mock.calls[0][1]).toEqual(api.correctInvoice.mock.calls[1][1]);
});


it('shows revision history and retained original values in the document workspace', async () => {
  const api = fakeApi(); const user = userEvent.setup(); const response = invoiceFixture();
  response.original_result = structuredClone(response.result);
  response.result.invoice.total = '110.00';
  response.result.evidence = [];
  response.correction_revision = 1; response.corrected_fields = ['total'];
  api.invoice.mockResolvedValue(response);
  api.validation.mockResolvedValue({ ...validationFixture(), correction_revision: 1 });
  api.corrections.mockResolvedValue({ document_id: ID, extraction_id: EXTRACTION, items: [{ ...receipt(), reviewer_label: 'Synthetic reviewer', reason: 'Verified source total', changed_fields: ['total'], original_digest: 'a'.repeat(64), tolerance: '0.02' }], total: 1, offset: 0, limit: 5, has_more: false });
  render(<DocumentDetail api={api} id={ID} />);
  expect(await screen.findByText('USD 110.00')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Show original extraction' }));
  expect(await screen.findByText('USD 120.00')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Show correction history' }));
  expect(await screen.findByText('Verified source total')).toBeInTheDocument();
  expect(api.validation).toHaveBeenCalledWith(ID, EXTRACTION, expect.any(AbortSignal), 1);
});


it('offers retry only for a genuinely failed job with a server-issued job ID', () => {
  const api = fakeApi(); const { rerender } = render(<RetryControl api={api} document={documentFixture()} reload={vi.fn()} />);
  expect(screen.queryByRole('button', { name: 'Retry failed job' })).not.toBeInTheDocument();
  rerender(<RetryControl api={api} document={documentFixture({ status: 'failed' })} reload={vi.fn()} />);
  expect(screen.queryByRole('button', { name: 'Retry failed job' })).not.toBeInTheDocument();
  rerender(<RetryControl api={api} document={documentFixture({ status: 'failed', failed_job_id: OTHER_ID })} reload={vi.fn()} />);
  expect(screen.getByRole('button', { name: 'Retry failed job' })).toBeEnabled();
});


it('suppresses double clicks and refreshes only after a retry receipt', async () => {
  enableIds();
  const api = fakeApi(); const reload = vi.fn();
  let resolve!: (value: Awaited<ReturnType<typeof api.reprocess>>) => void;
  api.reprocess.mockReturnValue(new Promise(done => { resolve = done; }));
  render(<RetryControl api={api} document={documentFixture({ status: 'failed', failed_job_id: OTHER_ID })} reload={reload} />);
  const button = screen.getByRole('button', { name: 'Retry failed job' });
  fireEvent.click(button); fireEvent.click(button);
  expect(api.reprocess).toHaveBeenCalledTimes(1);
  expect(reload).not.toHaveBeenCalled();
  await act(async () => { resolve({ request_id: REQUEST, document_id: ID, failed_job_id: OTHER_ID, new_job_id: EXTRACTION, created_at: '2026-10-30T09:00:00Z' }); });
  expect(reload).toHaveBeenCalledTimes(1);
});


it('reuses retry identity after a timeout and blocks a conflict until refreshed', async () => {
  enableIds();
  const api = fakeApi(); const reload = vi.fn(); const user = userEvent.setup();
  api.reprocess.mockRejectedValueOnce(new ApiError('Timed out.', 0, 'timeout')).mockRejectedValueOnce(new ApiError('Already running.', 409, 'job_not_retryable'));
  render(<RetryControl api={api} document={documentFixture({ status: 'failed', failed_job_id: OTHER_ID })} reload={reload} />);
  await user.click(screen.getByRole('button', { name: 'Retry failed job' }));
  expect(await screen.findByText('Timed out.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Retry failed job' }));
  expect(await screen.findByText('Already running.')).toBeInTheDocument();
  expect(api.reprocess.mock.calls[0][1]).toEqual(api.reprocess.mock.calls[1][1]);
  expect(screen.getByRole('button', { name: 'Retry failed job' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Refresh job status' }));
  expect(reload).toHaveBeenCalledTimes(1);
});
