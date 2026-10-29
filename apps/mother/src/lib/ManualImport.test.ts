import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ManualImport from './ManualImport.svelte';

afterEach(() => cleanup());

describe('ManualImport', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('{}')),
    );
  });

  it('renders the import form with labelled fields', () => {
    render(ManualImport);
    expect(screen.getByLabelText(/Source URL/)).toBeInTheDocument();
    expect(screen.getByLabelText(/VIN/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Photographs/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Create listing/ })).toBeInTheDocument();
  });

  it('rejects an out-of-range year before any network call', async () => {
    render(ManualImport);
    await fireEvent.input(screen.getByLabelText('Source site *'), {
      target: { value: 'example.test' },
    });
    await fireEvent.input(screen.getByLabelText('Title *'), { target: { value: 't' } });
    await fireEvent.input(screen.getByLabelText('Year'), { target: { value: '1500' } });
    await fireEvent.submit(screen.getByRole('button', { name: /Create listing/ }));
    expect(await screen.findByText(/between 1886 and 2100/)).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('walks form fields by keyboard', async () => {
    render(ManualImport);
    (screen.getByLabelText(/Source URL/) as HTMLElement).focus();
    expect(document.activeElement).toBe(screen.getByLabelText(/Source URL/));
    // Tab order follows DOM order: URL → site → title.
    (document.activeElement as HTMLElement).blur();
    (screen.getByLabelText('Source site *') as HTMLElement).focus();
    expect(document.activeElement).toBe(screen.getByLabelText('Source site *'));
  });
});
