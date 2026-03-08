import './App.css';

import Header from './components/Header';
import PageHeader from './components/PageHeader';
import IntegrationPanels from './components/IntegrationPanels';
import Sidebar from './components/Sidebar';
import BottomBar from './components/BottomBar';

function App() {
  return (
    <div className="min-h-screen font-sans text-neutral-800 pb-36">
      <Header />
      
      <PageHeader />

      {/* MAIN CONTENT GRID */}
      <main className="px-8 py-4 flex flex-col xl:flex-row gap-6">
        <IntegrationPanels />
        <Sidebar />
      </main>

      <BottomBar />
    </div>
  );
}

export default App;