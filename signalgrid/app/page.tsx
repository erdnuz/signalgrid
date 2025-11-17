"use client";

import { useEffect, useState } from "react";

interface Event {
  id: string;
  message: string;
}

export default function Home() {
  const [pulseEvent, setPulseEvent] = useState<Event | null>(null);
  const [forgeEvent, setForgeEvent] = useState<Event | null>(null);
  const [archiveEvent, setArchiveEvent] = useState<Event | null>(null);

  const fetchPulse = async () => {
    try {
      const res = await fetch("http://localhost:8001/events");
      if (!res.ok) throw new Error("Pulse fetch failed");
      const data: Event = await res.json();
      setPulseEvent(data);
    } catch (err) {
      console.error(err);
      setPulseEvent(null);
    }
  };

  const fetchForge = async () => {
    try {
      const res = await fetch("http://localhost:8002/forge"); // optional API endpoint for processed events
      if (!res.ok) throw new Error("Forge fetch failed");
      const data: Event = await res.json();
      setForgeEvent(data);
    } catch {
      setForgeEvent(null);
    }
  };

  const fetchArchive = async () => {
    try {
      const res = await fetch("http://localhost:8003/archive"); // Archive REST API
      if (!res.ok) throw new Error("Archive fetch failed");
      const data: Event = await res.json();
      setArchiveEvent(data);
    } catch {
      setArchiveEvent(null);
    }
  };

  useEffect(() => {
    fetchPulse();
    fetchForge();
    fetchArchive();
    const interval = setInterval(() => {
      fetchPulse();
      fetchForge();
      fetchArchive();
    }, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div style={{ padding: "2rem", fontFamily: "sans-serif" }}>
      <h1>SignalGrid Pipeline Demo</h1>

      <div>
        <h2>Pulse</h2>
        {pulseEvent ? (
          <div>
            <p><strong>ID:</strong> {pulseEvent.id}</p>
            <p><strong>Message:</strong> {pulseEvent.message}</p>
          </div>
        ) : <p>No event</p>}
      </div>

      <div>
        <h2>Forge</h2>
        {forgeEvent ? (
          <div>
            <p><strong>ID:</strong> {forgeEvent.id}</p>
            <p><strong>Message:</strong> {forgeEvent.message}</p>
          </div>
        ) : <p>No event processed yet</p>}
      </div>

      <div>
        <h2>Archive</h2>
        {archiveEvent ? (
          <div>
            <p><strong>ID:</strong> {archiveEvent.id}</p>
            <p><strong>Message:</strong> {archiveEvent.message}</p>
          </div>
        ) : <p>No event archived yet</p>}
      </div>
    </div>
  );
}
