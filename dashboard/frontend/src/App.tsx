import { lazy, Suspense } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { Navigation } from './components/Navigation';
import { DashboardPage } from './components/DashboardPage';
import { KanbanBoard } from './components/KanbanBoard';
import { CalendarPage } from './components/CalendarPage';
import { AgentChatPage } from './components/AgentChatPage';
import { JobsSubmittedPage } from './components/JobsSubmittedPage';
import { VolunteerPage } from './components/VolunteerPage';
import { CodebasePage } from './components/CodebasePage';
import { MorningPage } from './components/MorningPage';
import { RemindersPage } from './components/RemindersPage';
import { Spinner } from './components/Spinner';
import { PageBoundary } from './components/RouteErrorBoundary';
import { ProjectsProvider } from './hooks/ProjectsProvider';
import './App.css';

// The only pages that draw charts. recharts is about a third of the bundle,
// so these load on first visit instead of with every other page.
const AgenticDashboard = lazy(() =>
  import('./components/AgenticDashboard').then(m => ({ default: m.AgenticDashboard })));
const JobsPage = lazy(() =>
  import('./components/JobsPage').then(m => ({ default: m.JobsPage })));

function App() {
  return (
    <ProjectsProvider>
      <BrowserRouter>
        <div className="app">
          <Navigation />
          <PageBoundary>
            <Suspense fallback={<Spinner size="large" />}>
              <Routes>
                <Route path="/dashboard" element={<DashboardPage />} />
                <Route path="/kanban" element={<KanbanBoard />} />
                <Route path="/kanban/:project" element={<KanbanBoard />} />
                <Route path="/agentic" element={<AgenticDashboard />} />
                <Route path="/calendar" element={<CalendarPage />} />
                <Route path="/agent-chat" element={<AgentChatPage />} />
                <Route path="/jobs" element={<JobsPage />} />
                <Route path="/jobs/submitted" element={<JobsSubmittedPage />} />
                <Route path="/jobs/volunteer" element={<VolunteerPage />} />
                <Route path="/codebase" element={<CodebasePage />} />
                <Route path="/morning" element={<MorningPage />} />
                <Route path="/reminders" element={<RemindersPage />} />
                <Route path="*" element={<Navigate to="/kanban" replace />} />
              </Routes>
            </Suspense>
          </PageBoundary>
        </div>
      </BrowserRouter>
    </ProjectsProvider>
  );
}

export default App;
