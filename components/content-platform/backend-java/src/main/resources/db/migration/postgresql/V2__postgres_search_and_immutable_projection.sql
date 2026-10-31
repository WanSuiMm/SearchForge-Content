ALTER TABLE contents ADD COLUMN search_vector tsvector NOT NULL DEFAULT ''::tsvector;
UPDATE contents SET search_vector = to_tsvector('simple', body);
CREATE INDEX contents_search_vector_gin ON contents USING GIN (search_vector);

CREATE FUNCTION content_platform_search_vector_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.search_vector := to_tsvector('simple', NEW.body);
    RETURN NEW;
END;
$$;
CREATE TRIGGER contents_search_vector_change
BEFORE INSERT OR UPDATE OF body ON contents
FOR EACH ROW EXECUTE FUNCTION content_platform_search_vector_change();

CREATE FUNCTION content_platform_projection_event_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'projection events are immutable';
END;
$$;
CREATE TRIGGER projection_events_immutable
BEFORE UPDATE OR DELETE ON projection_events
FOR EACH ROW EXECUTE FUNCTION content_platform_projection_event_immutable();
