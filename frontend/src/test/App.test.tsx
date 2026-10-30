import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { ApiError } from '../api';
import { UploadPanel } from '../components';
import { useRemote } from '../hooks';
import { EXTRACTION, ID, OTHER_ID, documentFixture, fakeApi, limitsFixture, listFixture } from './fixtures';

beforeEach(() => { window.history.replaceState(null, '', '/#/'); });

it('lists real API records and submits search and status filters', async () => {
  const api = fakeApi(); const user = userEvent.setup();
  render(<App api={api} limits={limitsFixture} />);
  expect(await screen.findByRole('link', { name: /Open synthetic-review.pdf/ })).toHaveAttribute('href', '#/documents/' + ID);
  await user.type(screen.getByRole('searchbox', { name: 'Search documents' }), 'Supplier & invoice');
  await user.click(screen.getByRole('button', { name: 'Search' }));
  await waitFor(() => expect(api.documents).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'Supplier & invoice', offset: 0 }), expect.any(AbortSignal)));
  await user.selectOptions(screen.getByLabelText('Status'), 'failed');
  await waitFor(() => expect(api.documents).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'failed', offset: 0 }), expect.any(AbortSignal)));
});

it('uses the backend pagination offset rather than truncating local results', async () => {
  const api = fakeApi(); const user = userEvent.setup();
  api.documents.mockImplementation(async query => ({ ...listFixture(), total: 21, offset: query.offset, has_more: query.offset === 0 }));
  render(<App api={api} limits={limitsFixture} />);
  await screen.findByRole('link', { name: /Open synthetic-review.pdf/ });
  await user.click(screen.getByRole('button', { name: 'Next' }));
  await waitFor(() => expect(api.documents).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 20 }), expect.any(AbortSignal)));
});

it('uploads selected files and announces acceptance without claiming completion', async () => {
  const api = fakeApi(); const accepted = vi.fn(); const user = userEvent.setup();
  render(<UploadPanel api={api} limits={limitsFixture} onAccepted={accepted} />);
  const file = new File(['synthetic'], 'invoice.pdf', { type: 'application/pdf' });
  await user.upload(screen.getByLabelText('Select documents'), file);
  await user.click(screen.getByRole('button', { name: 'Upload 1 document' }));
  await waitFor(() => expect(accepted).toHaveBeenCalledWith(1));
  expect(api.upload).toHaveBeenCalledTimes(1);
  expect(api.upload.mock.calls[0][0][0].name).toBe('invoice.pdf');
  expect(screen.queryByRole('button', { name: 'Upload 1 document' })).not.toBeInTheDocument();
});

it('supports drag and drop and blocks unsupported files without discarding them silently', async () => {
  const api = fakeApi();
  render(<UploadPanel api={api} limits={limitsFixture} onAccepted={vi.fn()} />);
  fireEvent.drop(screen.getByText('Upload financial documents').closest('.drop-zone')!, {
    dataTransfer: { files: [new File(['bad'], 'program.exe')] },
  });
  expect(await screen.findByText('program.exe: unsupported file format.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Upload 1 document' })).toBeDisabled();
  expect(api.upload).not.toHaveBeenCalled();
});

it('does not automatically retry an uncertain upload', async () => {
  const api = fakeApi(); const user = userEvent.setup();
  api.upload.mockRejectedValue(new ApiError('Connection lost.', 0));
  render(<UploadPanel api={api} limits={limitsFixture} onAccepted={vi.fn()} />);
  await user.upload(screen.getByLabelText('Select documents'), new File(['pdf'], 'invoice.pdf'));
  await user.click(screen.getByRole('button', { name: 'Upload 1 document' }));
  expect(await screen.findByText(/server may have received the files/i)).toBeInTheDocument();
  expect(api.upload).toHaveBeenCalledTimes(1);
});

it('shows API failure and allows an explicit request retry', async () => {
  const api = fakeApi(); const user = userEvent.setup();
  api.documents.mockRejectedValueOnce(new ApiError('Synthetic backend outage.', 503)).mockResolvedValue(listFixture());
  render(<App api={api} limits={limitsFixture} />);
  expect(await screen.findByText('Synthetic backend outage.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Retry request' }));
  expect(await screen.findByRole('link', { name: /Open synthetic-review.pdf/ })).toBeInTheDocument();
});

it('displays invoice values, review issues, and duplicate links from the APIs', async () => {
  window.history.replaceState(null, '', '/#/documents/' + ID);
  const api = fakeApi();
  render(<App api={api} limits={limitsFixture} />);
  expect(await screen.findByText('USD 120.00')).toBeInTheDocument();
  expect(await screen.findByText('Synthetic arithmetic mismatch.')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: /Open related document/ })).toHaveAttribute('href', '#/documents/' + OTHER_ID);
  expect(screen.getByText(/Test-provider result/)).toBeInTheDocument();
  expect(screen.getAllByText('Not provided').length).toBeGreaterThan(0);
  expect(api.validation).toHaveBeenCalledWith(ID, EXTRACTION, expect.any(AbortSignal), 0);
});

it('opens cited source content with the exact extraction ID and sequence cursor', async () => {
  window.history.replaceState(null, '', '/#/documents/' + ID);
  const api = fakeApi(); const user = userEvent.setup();
  render(<App api={api} limits={limitsFixture} />);
  await user.click(await screen.findByRole('button', { name: 'View source for total' }));
  expect(await screen.findByLabelText('Extracted text for unit 3')).toHaveTextContent('Synthetic source text.');
  expect(screen.getByText('Synthetic OCR warning for test.')).toBeInTheDocument();
  expect(api.content).toHaveBeenCalledWith(ID, { after_sequence: 2, limit: 5, extraction_id: EXTRACTION }, expect.any(AbortSignal));
  await user.click(screen.getByRole('button', { name: 'Next units' }));
  await waitFor(() => expect(api.content).toHaveBeenLastCalledWith(ID, { after_sequence: 3, limit: 5, extraction_id: EXTRACTION }, expect.any(AbortSignal)));
});

it('handles an extraction change without combining content versions', async () => {
  window.history.replaceState(null, '', '/#/documents/' + ID);
  const api = fakeApi(); const user = userEvent.setup();
  api.content.mockRejectedValue(new ApiError('The processing attempt changed.', 409, 'extraction_changed'));
  render(<App api={api} limits={limitsFixture} />);
  await user.click(await screen.findByRole('button', { name: 'View source for total' }));
  expect(await screen.findByText('Document version changed')).toBeInTheDocument();
  expect(screen.queryByLabelText('Extracted text for unit 3')).not.toBeInTheDocument();
});

it('offers replacement upload when no retryable failed-job token is returned', async () => {
  window.history.replaceState(null, '', '/#/documents/' + ID);
  const api = fakeApi();
  api.document.mockResolvedValue(documentFixture({ status: 'failed', error_code: 'gemini_credentials_missing', error_message: 'Gemini credentials are missing.' }));
  render(<App api={api} limits={limitsFixture} />);
  expect(await screen.findByText('Gemini credentials are missing.')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Upload the original again' })).toHaveAttribute('href', '#/?upload=1');
  expect(api.invoice).not.toHaveBeenCalled();
});

it('renders filenames as text rather than HTML', async () => {
  const api = fakeApi(); const name = '<img src=x onerror=alert(1)>.pdf';
  api.documents.mockResolvedValue(listFixture([documentFixture({ original_filename: name })]));
  render(<App api={api} limits={limitsFixture} />);
  expect(await screen.findByText(name)).toBeInTheDocument();
  expect(document.querySelector('img')).toBeNull();
});

describe('polling and request isolation', () => {
  it('polls active work and stops when the caller reports completion', async () => {
    vi.useFakeTimers();
    const load = vi.fn<(signal: AbortSignal) => Promise<{ status: string }>>().mockResolvedValueOnce({ status: 'queued' }).mockResolvedValue({ status: 'completed' });
    const { result, unmount } = renderHook(() => useRemote({ key: 'job', load, poll: data => data?.status === 'queued' ? 4000 : false }));
    await act(async () => { await Promise.resolve(); });
    expect(result.current.data?.status).toBe('queued');
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(result.current.data?.status).toBe('completed');
    await act(async () => { await vi.advanceTimersByTimeAsync(12000); });
    expect(load).toHaveBeenCalledTimes(2);
    unmount();
  });
  it('discards a late response after the requested document changes', async () => {
    let resolveOld!: (value: string) => void;
    const old = new Promise<string>(resolve => { resolveOld = resolve; });
    const { result, rerender } = renderHook(({ id }) => useRemote<string>({ key: id, load: async () => id === 'old' ? old : 'new result' }), { initialProps: { id: 'old' } });
    rerender({ id: 'new' });
    await waitFor(() => expect(result.current.data).toBe('new result'));
    await act(async () => { resolveOld('stale result'); await Promise.resolve(); });
    expect(result.current.data).toBe('new result');
  });
});
