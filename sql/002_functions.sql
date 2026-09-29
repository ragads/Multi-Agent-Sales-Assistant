-- Vector retrieval used by the Search agent (FR-4.2, FR-4.3, FR-4.4)
create or replace function match_chunks(
  query_embedding vector(1536),
  match_count int default 6,
  filter_category text default null,
  min_similarity float default 0.35
) returns table (
  id uuid,
  content text,
  source_ref text,
  category text,
  similarity float
) language sql stable as $$
  select c.id,
         c.content,
         c.source_ref,
         c.category,
         1 - (c.embedding <=> query_embedding) as similarity
  from chunks c
  where (filter_category is null or c.category = filter_category)
    and 1 - (c.embedding <=> query_embedding) >= min_similarity
  order by c.embedding <=> query_embedding
  limit match_count;
$$;

-- Session expiry cleanup (FR-2.4)
create or replace function expire_sessions() returns int language plpgsql as $$
declare n int;
begin
  delete from messages
   where session_id in (select id from sessions where expires_at < now());
  update sessions set status = 'expired'
   where expires_at < now() and status <> 'expired';
  get diagnostics n = row_count;
  return n;
end;
$$;
